import os
import json
import logging
import urllib.request
import urllib.parse
import re
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
Tratti solo argomenti di finanza, economia, mercati e investimenti. Se qualcuno ti fa una domanda completamente fuori tema (non finanziaria), allora rispondi che sei fissato con la finanza e non parli d'altro. Ma se la domanda E gia di finanza, rispondi e basta, senza mai precisare di cosa non parli.

LUNGHEZZA: sii CONCISO, dai l'essenziale in 3-4 frasi e non dilungarti. NON chiedere se vuoi approfondire: rispondi e basta.

Formattazione: per spiegazioni usa paragrafi brevi; per liste o confronti usa bullet col trattino. Non usare mai il carattere | nelle risposte."""

SYSTEM_PROMPT_DATA = """Sei Scratch, assistente finanziario. Ti vengono forniti dati o risultati di ricerca.
Usali per rispondere con dati aggiornati. Non elencare i risultati grezzi: rielaborali con il tuo stile, semplice e chiaro.
Se i dati non contengono la risposta, dillo onestamente invece di inventare. Non commentare mai su argomenti che non tratti: rispondi solo a cio che ti viene chiesto.

LUNGHEZZA: sii CONCISO. Dai l'essenziale in poche righe (3-4 frasi al massimo per notizie e concetti). NON dilungarti e NON chiedere se vuoi approfondire: rispondi e basta.

FORMATTAZIONE:
- Per QUOTAZIONI e PREZZI: titolo in grassetto e bullet col trattino, ogni dato su una riga, risposta secca.
- Per LISTE/RICERCHE DI ETF: elenca i nomi trovati con bullet col trattino. Quando ti viene fornito un indirizzo web (che inizia con http), riportalo SEMPRE per intero e identico, lettera per lettera. NON sostituirlo mai con sigle, etichette o testo abbreviato. Il link deve comparire completo nella risposta.
- Per NOTIZIE e CONCETTI: poche righe essenziali e stop.

Non usare mai il carattere | nelle risposte."""

ROUTER_PROMPT = """Analizza la domanda di un utente in una chat di finanza. Rispondi SOLO con un oggetto JSON valido, niente altro.

Il JSON deve avere due campi:
- "categoria": una tra AZIONE, ETF, NOTIZIA, CONCETTO
- "titolo": dipende dalla categoria:
   * se AZIONE: il solo nome dell'azienda (es. "Microsoft", "Apple", "Eni")
   * se ETF e l'utente cerca ETF per tema/settore: il TEMA tradotto in inglese e conciso (es. "artificial intelligence", "renewable energy", "water", "cybersecurity", "semiconductors")
   * altrimenti: stringa vuota ""

Categorie:
- AZIONE: chiede il prezzo/quotazione di un'azione o titolo specifico
- ETF: qualsiasi cosa riguardi ETF (prezzo di uno specifico, ricerca per tema, confronti)
- NOTIZIA: notizie, eventi, analisi di mercato, dati macro, crypto
- CONCETTO: domanda teorica che non richiede dati aggiornati

Esempi:
Domanda: "Scratch quanto quota Microsoft?" -> {{"categoria": "AZIONE", "titolo": "Microsoft"}}
Domanda: "consigliami un ETF sulle energie rinnovabili" -> {{"categoria": "ETF", "titolo": "renewable energy"}}
Domanda: "ETF che investe in intelligenza artificiale" -> {{"categoria": "ETF", "titolo": "artificial intelligence"}}
Domanda: "un etf sulle aziende che producono infrastrutture per l'IA" -> {{"categoria": "ETF", "titolo": "artificial intelligence"}}
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


def trova_isin(testo: str) -> str:
    """Cerca un codice ISIN nel testo (2 lettere + 10 caratteri alfanumerici)."""
    match = re.search(r'\b[A-Z]{2}[A-Z0-9]{10}\b', testo.upper())
    return match.group(0) if match else None


def prezzo_etf_justetf(isin: str) -> str:
    """Prende il prezzo di un ETF dall'endpoint quote di JustETF (JSON)."""
    url = f"https://www.justetf.com/api/etfs/{isin}/quote?locale=it&currency=EUR&isin={isin}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        prezzo = data.get("latestQuote", {}).get("localized")
        var_pct = data.get("dtdPrc", {}).get("localized")
        low = data.get("quoteLowHigh", {}).get("low", {}).get("localized")
        high = data.get("quoteLowHigh", {}).get("high", {}).get("localized")
        venue = data.get("quoteTradingVenue", "")

        if not prezzo:
            logger.info(f"JustETF quote: nessun prezzo per {isin}")
            return "NESSUN_DATO"

        logger.info(f"JustETF quote OK: {isin} = {prezzo} EUR")
        righe = [f"ISIN: {isin}", f"Prezzo: {prezzo} EUR"]
        if var_pct:
            righe.append(f"Variazione giornaliera: {var_pct}%")
        if low and high:
            righe.append(f"Intervallo (min-max): {low} - {high} EUR")
        if venue:
            righe.append(f"Borsa: {venue}")
        return "\n".join(righe)
    except Exception as e:
        logger.error(f"Errore JustETF quote: {e}")
        return "ERRORE_DATI"


def leggi_pagina_justetf(isin: str) -> str:
    """Estrae le info descrittive dell'ETF dalla pagina JustETF via Tavily."""
    url = f"https://www.justetf.com/it/etf-profile.html?isin={isin}"
    try:
        risultato = tavily_client.extract(urls=[url])
        contenuti = risultato.get("results", [])
        if contenuti:
            testo = contenuti[0].get("raw_content", "") or contenuti[0].get("content", "")
            if testo:
                logger.info(f"JustETF info OK per ISIN {isin}")
                return testo[:1800]
        return "NESSUN_DATO"
    except Exception as e:
        logger.error(f"Errore JustETF extract: {e}")
        return "ERRORE_DATI"


def link_justetf_tema(tema: str) -> str:
    """Link alla pagina di ricerca JustETF per un tema, dove ci sono gli ISIN."""
    q = urllib.parse.quote_plus(tema)
    return f"https://www.justetf.com/it/search.html?query={q}"


def cerca_etf_tematici(tema: str, testo_originale: str) -> str:
    """Cerca ETF per tema via Tavily su JustETF/Morningstar e aggiunge il link alla lista JustETF."""
    risultati = cerca_tavily(testo_originale, ETF_SITES, max_results=5)
    link = link_justetf_tema(tema)
    istruzione = (
        f"\n\nIMPORTANTE: alla fine indica che la lista completa con tutti i codici ISIN e su JustETF a questo "
        f"indirizzo, da riportare ESATTAMENTE e per intero cosi com'e (senza abbreviarlo): {link}"
    )
    if risultati in ("NESSUN_DATO", "ERRORE_DATI"):
        return f"Nessun nome specifico trovato.{istruzione}"
    return f"{risultati}{istruzione}"


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


def vuole_approfondire(testo: str) -> bool:
    """Capisce se l'utente sta chiedendo di approfondire/espandere la risposta."""
    t = testo.lower()
    frasi = [
        "approfondisci", "approfondire", "dimmi di piu", "dimmi di più", "piu dettagli",
        "più dettagli", "piu informazioni", "più informazioni", "spiega meglio",
        "spiegami meglio", "vai nel dettaglio", "nel dettaglio", "piu nel dettaglio",
        "più nel dettaglio", "raccontami di piu", "raccontami di più", "estenditi",
        "piu dettagliata", "più dettagliata", "elabora", "in dettaglio",
    ]
    return any(f in t for f in frasi)


async def rispondi(chat_id: int, user_message: str, nome: str, context_data: str = None, lungo: bool = False) -> str:
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

        max_tok = 800 if lungo else 250
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=max_tok,
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
        isin = trova_isin(testo)
        if isin:
            # ISIN presente: prezzo dall'endpoint quote + info descrittive dalla pagina
            prezzo = prezzo_etf_justetf(isin)
            info = leggi_pagina_justetf(isin)
            parti = []
            if prezzo not in ("NESSUN_DATO", "ERRORE_DATI"):
                parti.append("QUOTAZIONE:\n" + prezzo)
            if info not in ("NESSUN_DATO", "ERRORE_DATI"):
                parti.append("INFORMAZIONI FONDO:\n" + info)
            if parti:
                context_data = "\n\n".join(parti)
            else:
                context_data = cerca_tavily(testo, ETF_SITES, max_results=3)
        else:
            # Nessun ISIN: ricerca tematica via Tavily + link alla lista JustETF con gli ISIN
            tema = titolo if titolo else testo
            context_data = cerca_etf_tematici(tema, testo)
    elif categoria == "NOTIZIA":
        context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)

    if context_data in ("NESSUN_DATO", "ERRORE_DATI"):
        context_data = None

    # Sempre conciso
    reply = await rispondi(chat_id, testo, nome, context_data=context_data, lungo=False)
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
