import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
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

IMPORTANTE: se la domanda e vaga e non contiene un tema specifico (es. "info su questo ETF", "quelli di prima"), metti titolo: "".

Esempi:
Domanda: "Scratch quanto quota Microsoft?" -> {{"categoria": "AZIONE", "titolo": "Microsoft"}}
Domanda: "consigliami un ETF sulle energie rinnovabili" -> {{"categoria": "ETF", "titolo": "renewable energy"}}
Domanda: "ETF che investe in intelligenza artificiale" -> {{"categoria": "ETF", "titolo": "artificial intelligence"}}
Domanda: "un etf sulle aziende che producono infrastrutture per l'IA" -> {{"categoria": "ETF", "titolo": "artificial intelligence infrastructure"}}
Domanda: "perche sale il bitcoin" -> {{"categoria": "NOTIZIA", "titolo": ""}}
Domanda: "cos'e un dividendo" -> {{"categoria": "CONCETTO", "titolo": ""}}
Domanda: "info su questo ETF" -> {{"categoria": "ETF", "titolo": ""}}
Domanda: "quelli che mi hai suggerito" -> {{"categoria": "ETF", "titolo": ""}}

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
        # Sicurezza: se il titolo e la domanda intera o quasi, ignoralo
        if len(titolo) > 40:
            titolo = ""
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
        q = urllib.parse.quote(titolo)
        lookup = _http_get_json(
            f"https://finnhub.io/api/v1/search?q={q}&token={FINNHUB_API_KEY}"
        )
        risultati = lookup.get("result", [])
        if not risultati:
            logger.info(f"Finnhub: nessun simbolo per '{titolo}'")
            return "NESSUN_DATO"
        symbol = risultati[0].get("symbol")
        descr = risultati[0].get("description", symbol)
        for r in risultati:
            s = r.get("symbol", "")
            if "." not in s:
                symbol = s
                descr = r.get("description", s)
                break

        quote = _http_get_json(
            f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        )
        prezzo = quote.get("c")
        prev = quote.get("pc")
        high = quote.get("h")
        low = quote.get("l")

        if not prezzo:
            logger.info(f"Finnhub: nessun prezzo per {symbol}")
            return "NESSUN_DATO"

        logger.info(f"Finnhub OK: {symbol} = {prezzo}")
        righe = [f"**{descr} ({symbol})**", f"- Prezzo: {round(prezzo, 2)} USD"]
        if prezzo and prev:
            pct = ((prezzo - prev) / prev) * 100
            righe.append(f"- Variazione: {pct:+.2f}% rispetto alla chiusura precedente")
        if high and low:
            righe.append(f"- Intervallo giornaliero: {round(low, 2)} - {round(high, 2)} USD")
        return "\n".join(righe)
    except Exception as e:
        logger.error(f"Errore Finnhub: {e}")
        return "ERRORE_DATI"


def trova_isin(testo: str) -> str:
    """Cerca un codice ISIN nel testo. Deve contenere almeno una cifra."""
    for match in re.finditer(r'\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b', testo.upper()):
        candidato = match.group(0)
        if any(c.isdigit() for c in candidato):
            return candidato
    for match in re.finditer(r'\b[A-Z]{2}[A-Z0-9]{10}\b', testo.upper()):
        candidato = match.group(0)
        if any(c.isdigit() for c in candidato):
            return candidato
    return None


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
        righe = [f"ISIN: {isin}", f"- Prezzo: {prezzo} EUR"]
        if var_pct:
            righe.append(f"- Variazione giornaliera: {var_pct}%")
        if low and high:
            righe.append(f"- Intervallo (min-max): {low} - {high} EUR")
        if venue:
            righe.append(f"- Borsa: {venue}")
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
    """Link alla pagina di ricerca JustETF per un tema."""
    q = urllib.parse.quote_plus(tema)
    return f"https://www.justetf.com/it/search.html?query={q}&search=ALL"


def cerca_etf_screener(tema: str) -> list:
    """
    Cerca ETF per tema usando l'endpoint interno di JustETF.
    Strategia: carica prima la pagina per ottenere i cookie di sessione Wicket,
    poi fa la chiamata AJAX con quei cookie.
    Ritorna lista di dict {name, isin, ter, fundSize, yearReturn} o lista vuota.
    """
    tema_q = urllib.parse.quote_plus(tema)
    page_url = f"https://www.justetf.com/it/search.html?query={tema_q}&search=ALL"
    # Endpoint AJAX interno — il path del componente Wicket è fisso per pagine nuove
    ajax_url = (
        "https://www.justetf.com/it/search.html?"
        "1-1.0-container-tabsContentContainer-tabsContentRepeater-0-container-content-"
        "container-resultContent-etfsContainer-etfsTablePanel"
        f"&query={tema_q}&search=ALL&_wicket=1"
    )
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        # Step 1: carica la pagina di ricerca per ottenere i cookie di sessione
        req_page = urllib.request.Request(page_url, headers={
            **base_headers,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        opener.open(req_page, timeout=12)

        # Step 2: chiama l'endpoint AJAX con i cookie appena ottenuti
        req_ajax = urllib.request.Request(ajax_url, headers={
            **base_headers,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": f"search.html?query={tema_q}&search=ALL",
            "Referer": page_url,
        })
        with opener.open(req_ajax, timeout=15) as resp:
            data = json.loads(resp.read())

        risultati = []
        for etf in data.get("data", [])[:6]:
            isin_val = etf.get("isin", "")
            name_val = etf.get("name", "")
            if isin_val and name_val:
                risultati.append({
                    "name": name_val,
                    "isin": isin_val,
                    "ter": etf.get("ter", "n/d"),
                    "fundSize": etf.get("fundSize", "n/d"),
                    "yearReturn": etf.get("yearReturnCUR", "n/d"),
                })

        logger.info(f"Screener JustETF OK: {len(risultati)} ETF per tema '{tema}'")
        return risultati

    except Exception as e:
        logger.error(f"Errore screener JustETF: {e}")
        return []


def cerca_etf_tematici_tavily(tema: str, testo_originale: str) -> str:
    """Fallback: cerca ETF per tema via Tavily se lo screener non risponde."""
    risultati = cerca_tavily(testo_originale, ETF_SITES, max_results=5)
    if risultati in ("NESSUN_DATO", "ERRORE_DATI"):
        return "Nessun ETF specifico trovato. Prova a specificare meglio il tema."
    nota = "\n\n(Non scrivere tu nessun link o indirizzo web: verra aggiunto automaticamente dopo.)"
    return f"{risultati}{nota}"


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


def is_domanda_prezzo(testo: str) -> bool:
    """Capisce se la domanda riguarda il prezzo/quotazione o aspetti qualitativi."""
    parole_prezzo = ["quota", "quotazione", "prezzo", "price", "valore", "vale", "quanto costa", "oggi"]
    return any(p in testo.lower() for p in parole_prezzo)


async def rispondi(chat_id: int, user_message: str, nome: str, context_data: str = None, testo_in_reply: str = "") -> str:
    try:
        contesto_reply = ""
        if testo_in_reply:
            contesto_reply = f"\n\n(L'utente sta rispondendo a questo messaggio precedente, usalo come contesto:\n{testo_in_reply})"
        if context_data:
            system = SYSTEM_PROMPT_DATA
            history = get_history(chat_id, limit=3)
            user_content = f"Domanda di {nome}: {user_message}{contesto_reply}\n\nDati disponibili:\n{context_data}"
        else:
            system = SYSTEM_PROMPT
            history = get_history(chat_id, limit=5)
            user_content = f"[Messaggio di {nome}]: {user_message}{contesto_reply}"

        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=250,
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
        and (message.reply_to_message.from_user.username or "").lower() == "scratchfinbot"
    )
    if BOT_NAME not in testo and not is_reply_to_bot:
        return

    # Testo del messaggio a cui si risponde (reply)
    testo_in_reply = ""
    if message.reply_to_message is not None and message.reply_to_message.text:
        testo_in_reply = message.reply_to_message.text

    # Testo esteso per la ricerca dell'ISIN: messaggio attuale + eventuale reply
    testo_esteso = (testo + "\n" + testo_in_reply).strip() if testo_in_reply else testo

    categoria, titolo = classifica_ed_estrai(testo)
    logger.info(f"Categoria: {categoria} | Titolo: '{titolo}' | Domanda: {testo[:60]}")

    context_data = None
    link_etf_da_aggiungere = None

    if categoria == "AZIONE":
        nome_titolo = titolo if titolo else testo
        dato = get_prezzo_azione(nome_titolo)
        if dato in ("NESSUN_DATO", "ERRORE_DATI"):
            context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)
        else:
            context_data = dato

    elif categoria == "ETF":
        isin = trova_isin(testo_esteso)
        if isin:
            # ISIN trovato: prezzo + info qualitative
            # Distingue domande sul prezzo da domande sulla composizione
            e_domanda_prezzo = is_domanda_prezzo(testo_esteso)
            prezzo = prezzo_etf_justetf(isin)
            info = leggi_pagina_justetf(isin)
            parti = []
            if e_domanda_prezzo:
                # Mette il prezzo prima
                if prezzo not in ("NESSUN_DATO", "ERRORE_DATI"):
                    parti.append("QUOTAZIONE:\n" + prezzo)
                if info not in ("NESSUN_DATO", "ERRORE_DATI"):
                    parti.append("INFORMAZIONI FONDO:\n" + info)
            else:
                # Domanda qualitativa: mette prima le info descrittive
                if info not in ("NESSUN_DATO", "ERRORE_DATI"):
                    parti.append("INFORMAZIONI FONDO:\n" + info)
                if prezzo not in ("NESSUN_DATO", "ERRORE_DATI"):
                    parti.append("QUOTAZIONE ATTUALE:\n" + prezzo)
            if parti:
                context_data = "\n\n".join(parti)
            else:
                context_data = cerca_tavily(testo, ETF_SITES, max_results=3)

        elif titolo:
            # Ricerca tematica: usa lo screener JustETF per avere gli ISIN veri
            link = link_justetf_tema(titolo)
            etf_list = cerca_etf_screener(titolo)

            if etf_list:
                righe = []
                for e in etf_list:
                    righe.append(
                        f"- {e['name']}\n  ISIN: {e['isin']} | TER: {e['ter']} | Rendimento 1Y: {e['yearReturn']} | Dimensione: {e['fundSize']}M EUR"
                    )
                context_data = (
                    f"ETF trovati per il tema '{titolo}':\n"
                    + "\n".join(righe)
                    + "\n\n(Non scrivere tu nessun link: viene aggiunto automaticamente dopo.)"
                )
                link_etf_da_aggiungere = link
            else:
                # Fallback su Tavily se lo screener non risponde
                logger.info(f"Screener fallito, fallback Tavily per tema '{titolo}'")
                context_data = cerca_etf_tematici_tavily(titolo, testo)
                link_etf_da_aggiungere = link

        else:
            # Domanda ETF vaga senza ISIN né tema
            reply = "Di quale ETF parli? Dimmi il nome o il codice ISIN e ti do prezzo e dati."
            add_to_history(chat_id, "user", f"[{nome}]: {testo}")
            add_to_history(chat_id, "assistant", reply)
            await message.reply_text(reply)
            return

    elif categoria == "NOTIZIA":
        context_data = cerca_tavily(testo, NEWS_SITES, max_results=3)

    if context_data in ("NESSUN_DATO", "ERRORE_DATI"):
        context_data = None

    reply = await rispondi(chat_id, testo, nome, context_data=context_data, testo_in_reply=testo_in_reply)

    # Il link JustETF lo attacca il CODICE, non llama
    if link_etf_da_aggiungere:
        reply = f"{reply}\n\nLista completa con tutti gli ISIN su JustETF:\n{link_etf_da_aggiungere}"

    await message.reply_text(reply, parse_mode="Markdown", disable_web_page_preview=True)


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
