import os
import re
import json
import logging
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
BOT_NAME        = "Scratch"
GROQ_MODEL      = "llama-3.3-70b-versatile"
CREATOR_USER    = "d4v3dt"

# Siti affidabili per notizie e macro
NEWS_SITES = [
    "ilsole24ore.com", "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "cnbc.com", "marketwatch.com", "investing.com", "milanofinanza.it",
    "borsaitaliana.it", "bankitalia.it", "ecb.europa.eu",
    "coindesk.com", "cointelegraph.com",
]
ETF_SITES = ["justetf.com", "morningstar.it"]

SYSTEM_PROMPT = """Sei Scratch, assistente finanziario in una chat di gruppo. Parli semplice, chiaro, alla portata di tutti.
Parli solo di finanza, economia, mercati e soldi. Se ti chiedono altro, dì che sei fissato con la finanza e non vuoi parlare d'altro.
Risposte concise ma complete. Non usare mai il carattere | nelle risposte. Mai muri di testo."""

SYSTEM_PROMPT_DATA = """Sei Scratch, assistente finanziario. Ti vengono forniti dati o risultati di ricerca.
Usali per rispondere con dati aggiornati. Non elencare i risultati grezzi: rielaborali con il tuo stile, semplice e chiaro.
Se i dati non contengono la risposta, dillo onestamente invece di inventare. Parli solo di finanza.
Non usare mai il carattere | nelle risposte. Mai muri di testo."""

ROUTER_PROMPT = """Classifica la seguente domanda di un utente in UNA di queste categorie. Rispondi SOLO con la parola della categoria, niente altro.

Categorie:
- AZIONE: chiede il prezzo/quotazione di un'azione o titolo specifico (es. "quanto quota Apple", "prezzo Eni")
- ETF: qualsiasi cosa riguardi ETF — prezzo di un ETF, lista di ETF su un tema, confronti tra ETF (es. "quota questo ETF IE00...", "lista ETF rinnovabili")
- NOTIZIA: chiede notizie, eventi, analisi di mercato, dati macroeconomici, crypto (es. "perché è sceso il mercato", "notizie su Bitcoin")
- CONCETTO: domanda teorica o di spiegazione che non richiede dati aggiornati (es. "cos'è un ETF", "come funziona un'obbligazione")

Domanda: {domanda}

Categoria:"""

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


def classifica_domanda(domanda: str) -> str:
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": ROUTER_PROMPT.format(domanda=domanda)}],
            max_tokens=10,
            temperature=0.0,
        )
        cat = response.choices[0].message.content.strip().upper()
        for c in ["AZIONE", "ETF", "NOTIZIA", "CONCETTO"]:
            if c in cat:
                return c
        return "CONCETTO"
    except Exception as e:
        logger.error(f"Errore router: {e}")
        return "CONCETTO"


def get_prezzo_azione(query: str) -> str:
    """Cerca il prezzo di un'azione via Yahoo Finance (URL diretto)."""
    import urllib.request
    try:
        # Prima trova il simbolo dal nome
        search_url = f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        quotes = data.get("quotes", [])
        if not quotes:
            return "NESSUN_DATO"
        symbol = quotes[0].get("symbol")
        nome = quotes[0].get("shortname", symbol)

        # Poi prendi il prezzo
        chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        req2 = urllib.request.Request(chart_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=10) as resp:
            chart = json.loads(resp.read())
        result = chart["chart"]["result"][0]
        meta = result["meta"]
        prezzo = meta.get("regularMarketPrice")
        valuta = meta.get("currency", "")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        var = ""
        if prezzo and prev:
            pct = ((prezzo - prev) / prev) * 100
            var = f" ({pct:+.2f}% rispetto a ieri)"
        return f"{nome} ({symbol}): {prezzo} {valuta}{var}"
    except Exception as e:
        logger.error(f"Errore Yahoo: {e}")
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
            max_tokens=600,
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
    await update.message.reply_text("Scratch è operativo. Parliamo di finanza.")


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

    categoria = classifica_domanda(testo)
    logger.info(f"Categoria: {categoria} | Domanda: {testo[:50]}")

    context_data = None

    if categoria == "AZIONE":
        dato = get_prezzo_azione(testo)
        if dato in ("NESSUN_DATO", "ERRORE_DATI"):
            # ripiego: prova come ricerca notizie
            context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)
        else:
            context_data = dato

    elif categoria == "ETF":
        n = 5 if is_lista(testo) else 3
        context_data = cerca_tavily(testo, ETF_SITES, max_results=n)

    elif categoria == "NOTIZIA":
        context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)

    # CONCETTO: nessuna ricerca, context_data resta None

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
    logger.info("Scratch è online.")
    app.run_polling()


if __name__ == "__main__":
    main()
