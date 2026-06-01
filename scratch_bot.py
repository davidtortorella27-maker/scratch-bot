import os
import json
import logging
import urllib.request
import urllib.parse
from collections import deque
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from groq import Groq
from tavily import TavilyClient

# ── Configurazione ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "IL_TUO_TOKEN_TELEGRAM")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY",   "LA_TUA_API_KEY_GROQ")
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY", "LA_TUA_API_KEY_TAVILY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "LA_TUA_API_KEY_FINNHUB")
BOT_NAME        = "Scratch"
GROQ_MODEL      = "llama-3.3-70b-versatile"
MODEL_ROUTER    = "llama-3.1-8b-instant"
CREATOR_USER    = "d4v3dt"

NEWS_SITES = [
    "ilsole24ore.com", "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "cnbc.com", "marketwatch.com", "investing.com", "milanofinanza.it",
    "borsaitaliana.it", "bankitalia.it", "ecb.europa.eu",
    "coindesk.com", "cointelegraph.com",
]
ETF_SITES = ["justetf.com", "morningstar.it"]

SYSTEM_PROMPT = """Sei Scratch, assistente finanziario in una chat di gruppo. Parli semplice, chiaro, alla portata di tutti.
Parli solo di finanza, economia, mercati e soldi. Se ti chiedono altro, di' che sei fissato con la finanza e non vuoi parlare d'altro.
Formattazione: per spiegazioni e concetti usa paragrafi discorsivi brevi; per liste, dati o confronti usa bullet point col trattino e titoli in grassetto.
IMPORTANTE: risposte brevi, massimo 5-6 righe. Mai muri di testo. Non usare mai il carattere | nelle risposte."""

SYSTEM_PROMPT_DATA = """Sei Scratch, assistente finanziario. Ti vengono forniti dati o risultati di ricerca.
Usali per rispondere con dati aggiornati. Non elencare i risultati grezzi: rielaborali con il tuo stile, semplice e chiaro.
Se i dati non contengono la risposta, dillo onestamente invece di inventare. Parli solo di finanza.

FORMATTAZIONE ADATTIVA:
- Per DATI, NUMERI, QUOTAZIONI, LISTE o CONFRONTI: titolo in grassetto e bullet point col trattino, ogni dato su una riga.
- Per CONCETTI o NOTIZIE: paragrafi discorsivi brevi, niente bullet forzati.

IMPORTANTE: risposte brevi, massimo 5-6 righe. Mai muri di testo. Non usare mai il carattere | nelle risposte."""

ROUTER_PROMPT = """Analizza la domanda di un utente in una chat di finanza. Rispondi SOLO con un oggetto JSON valido, niente altro.

Il JSON deve avere due campi:
- "categoria": una tra AZIONE, ETF, NOTIZIA, CONCETTO
- "titolo": se categoria e AZIONE, il solo nome dell'azienda o titolo (es. "Microsoft", "Apple", "Eni"); altrimenti stringa vuota ""

Categorie:
- AZIONE: chiede il prezzo/quotazione di un'azione o titolo specifico
- ETF: qualsiasi cosa riguardi ETF (prezzo, lista tematica, confronti)
- NOTIZIA: notizie, eventi, analisi di mercato, dati macro, crypto
- CONCETTO: domanda teorica che non richiede dati aggiornati

Esempi:
Domanda: "Scratch quanto quota Microsoft?" -> {{"categoria": "AZIONE", "titolo": "Microsoft"}}
Domanda: "lista ETF rinnovabili" -> {{"categoria": "ETF", "titolo": ""}}
Domanda: "perche sale il bitcoin" -> {{"categoria": "NOTIZIA", "titolo": ""}}
Domanda: "cos'e un dividendo" -> {{"categoria": "CONCETTO", "titolo": ""}}

Domanda: "{domanda}"

JSON:"""

# ── Stato globale ───────────────────────────────────────────────────────────────
bot_awake: dict[int, bool] = {}
chat_history: dict = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)


# ── Helpers ─────────────────────────────────────────────────────────────────────
def is_awake(chat_id: int) -> bool:
    return bot_awake.get(chat_id, False)


def get_history(chat_id: int, limit: int = 5) -> list:
    h = list(chat_history.get(chat_id, deque()))
    return h[-limit:]


def add_to_history(chat_id: int, role: str, content: str):
    if chat_id not in chat_history:
        chat_history[chat_id] = deque(maxlen=5)
    chat_history[chat_id].append({"role": role, "content": content})


def classifica_ed_estrai(domanda: str) -> tuple:
    try:
        response = groq_client.chat.completions.create(
            model=MODEL_ROUTER,
            messages=[{"role": "user", "content": ROUTER_PROMPT.format(domanda=domanda)}],
            max_tokens=50,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        cat = data.get("categoria", "CONCETTO").upper()
        titolo = data.get("titolo", "").strip()
        if cat not in ("AZIONE", "ETF", "NOTIZIA", "CONCETTO"):
            cat = "CONCETTO"
        return cat, titolo
    except Exception as e:
        logger.error(f"Errore router: {e}")
        return "CONCETTO", ""


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_prezzo_azione(titolo: str) -> str:
    """Cerca il prezzo via Finnhub: prima trova il simbolo, poi la quotazione."""
    try:
        # 1) Trova il simbolo dal nome
        q = urllib.parse.quote(titolo)
        lookup = _http_get_json(
            f"https://finnhub.io/api/v1/search?q={q}&token={FINNHUB_API_KEY}"
        )
        risultati = lookup.get("result", [])
        if not risultati:
            logger.info(f"Finnhub: nessun simbolo per '{titolo}'")
            return "NESSUN_DATO"
        # preferisce simboli senza punto (azioni USA pure) se presenti
        symbol = risultati[0].get("symbol")
        descr = risultati[0].get("description", symbol)
        for r in risultati:
            s = r.get("symbol", "")
            if "." not in s:
                symbol = s
                descr = r.get("description", s)
                break

        # 2) Prendi la quotazione
        quote = _http_get_json(
            f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        )
        prezzo = quote.get("c")   # current price
        prev = quote.get("pc")    # previous close
        high = quote.get("h")     # day high
        low = quote.get("l")      # day low

        if not prezzo:
            logger.info(f"Finnhub: nessun prezzo per {symbol}")
            return "NESSUN_DATO"

        logger.info(f"Finnhub OK: {symbol} = {prezzo}")
        righe = [f"Titolo: {descr} ({symbol})", f"Prezzo: {round(prezzo, 2)} USD"]
        if prezzo and prev:
            pct = ((prezzo - prev) / prev) * 100
            righe.append(f"Variazione: {pct:+.2f}% rispetto alla chiusura precedente")
        if high and low:
            righe.append(f"Intervallo giornaliero: {round(low, 2)} - {round(high, 2)} USD")
        return "\n".join(righe)
    except Exception as e:
        logger.error(f"Errore Finnhub: {e}")
        return "ERRORE_DATI"


def cerca_tavily(query: str, sites: list, max_results: int = 3) -> str:
    try:
        risultato = tavily_client.search(
            query=query,
            search_depth="advanced",
            max_results=max_results,
            include_answer=True,
            include_domains=sites,
        )
        parti = []
        if risultato.get("answer"):
            parti.append(f"Sintesi: {risultato['answer']}")
        for r in risultato.get("results", [])[:max_results]:
            titolo = r.get("title", "")
            contenuto = r.get("content", "")[:600]
            parti.append(f"- {titolo}: {contenuto}")
        return "\n".join(parti) if parti else "NESSUN_DATO"
    except Exception as e:
        logger.error(f"Errore Tavily: {e}")
        return "ERRORE_DATI"


def is_lista(testo: str) -> bool:
    parole = ["lista", "quali", "migliori", "elenco", "consigli", "consiglia", "suggerisci"]
    return any(p in testo.lower() for p in parole)


async def rispondi(chat_id: int, user_message: str, nome: str, context_data: str = None) -> str:
    try:
        if context_data:
            system = SYSTEM_PROMPT_DATA
            history = get_history(chat_id, limit=3)
            user_content = f"Domanda di {nome}: {user_message}\n\nDati disponibili:\n{context_data}"
        else:
            system = SYSTEM_PROMPT
            history = get_history(chat_id, limit=5)
            user_content = f"[Messaggio di {nome}]: {user_message}"

        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=400,
            temperature=0.7,
        )
        reply = response.choices[0].message.content.strip()

        add_to_history(chat_id, "user", f"[{nome}]: {user_message}")
        add_to_history(chat_id, "assistant", reply)
        return reply
    except Exception as e:
        logger.error(f"Errore Groq: {e}")
        return "Ho un problema tecnico. Riprova tra poco."


# ── Handlers ────────────────────────────────────────────────────────────────────
async def cmd_sveglia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bot_awake[chat_id] = True
    await update.message.reply_text("Scratch e operativo. Parliamo di finanza.")


async def cmd_dormi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    username = (update.message.from_user.username or "").lower()
    if username != CREATOR_USER:
        await update.message.reply_text("Non hai i permessi per farlo.")
        return
    bot_awake[chat_id] = False
    await update.message.reply_text("Scratch offline. A presto.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message  = update.message

    if not message or not message.text:
        return
    if not is_awake(chat_id):
        return

    nome = message.from_user.first_name or "Anonimo"
    testo = message.text

    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.is_bot
    )
    if BOT_NAME not in testo and not is_reply_to_bot:
        return

    categoria, titolo = classifica_ed_estrai(testo)
    logger.info(f"Categoria: {categoria} | Titolo: '{titolo}' | Domanda: {testo[:50]}")

    context_data = None

    if categoria == "AZIONE":
        nome_titolo = titolo if titolo else testo
        dato = get_prezzo_azione(nome_titolo)
        if dato in ("NESSUN_DATO", "ERRORE_DATI"):
            context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)
        else:
            context_data = dato
    elif categoria == "ETF":
        n = 5 if is_lista(testo) else 3
        context_data = cerca_tavily(testo, ETF_SITES, max_results=n)
    elif categoria == "NOTIZIA":
        context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)

    if context_data in ("NESSUN_DATO", "ERRORE_DATI"):
        context_data = None

    reply = await rispondi(chat_id, testo, nome, context_data=context_data)
    await message.reply_text(reply, parse_mode="Markdown")


# ── Main ────────────────────────────────────────────────────────────────────────
def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("sveglia", cmd_sveglia))
    app.add_handler(CommandHandler("dormi",   cmd_dormi))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Scratch e online.")
    app.run_polling()


if __name__ == "__main__":
    main()
