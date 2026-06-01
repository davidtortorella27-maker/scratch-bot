import os
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
WEB_KEYWORDS    = ["internet", "web", "online"]

SYSTEM_PROMPT = """Sei Scratch, assistente finanziario in una chat di gruppo. Parli semplice, chiaro, alla portata di tutti.
Parli solo di finanza, economia, mercati e soldi. Se ti chiedono altro, dì che sei fissato con la finanza e non vuoi parlare d'altro.
Risposte concise ma complete. Non usare mai il carattere | nelle risposte. Mai muri di testo."""

SYSTEM_PROMPT_WEB = """Sei Scratch, assistente finanziario in una chat di gruppo. Parli semplice, chiaro, alla portata di tutti.
Ti vengono forniti dei risultati di ricerca dal web. Usali per rispondere alla domanda con dati aggiornati.
Non elencare i risultati grezzi: rielaborali in una risposta naturale e scorrevole con il tuo stile.
Parli solo di finanza. Risposte concise ma complete. Non usare mai il carattere | nelle risposte. Mai muri di testo."""

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


def needs_web(testo: str) -> bool:
    return any(kw in testo.lower() for kw in WEB_KEYWORDS)


def clean_keywords(testo: str) -> str:
    parole = testo.split()
    parole_pulite = [p for p in parole if p.lower() not in WEB_KEYWORDS]
    return " ".join(parole_pulite).strip()


def cerca_web(query: str) -> str:
    try:
        risultato = tavily_client.search(
            query=query,
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )
        parti = []
        if risultato.get("answer"):
            parti.append(f"Sintesi: {risultato['answer']}")
        for r in risultato.get("results", [])[:3]:
            titolo = r.get("title", "")
            contenuto = r.get("content", "")[:400]
            parti.append(f"- {titolo}: {contenuto}")
        return "\n".join(parti) if parti else "Nessun risultato trovato."
    except Exception as e:
        logger.error(f"Errore Tavily: {e}")
        return "ERRORE_RICERCA"


async def ask_groq(chat_id: int, user_message: str, nome: str, web_context: str = None) -> str:
    try:
        if web_context:
            system = SYSTEM_PROMPT_WEB
            history = get_history(chat_id, limit=3)  # memoria corta in modalità web
            user_content = f"Domanda di {nome}: {user_message}\n\nRisultati dal web:\n{web_context}"
        else:
            system = SYSTEM_PROMPT
            history = get_history(chat_id, limit=5)  # memoria piena in modalità normale
            user_content = f"[Messaggio di {nome}]: {user_message}"

        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=500,
            temperature=0.7,
        )
        reply = response.choices[0].message.content.strip()

        # Salva sempre in memoria (sia normale che web)
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

    if needs_web(testo):
        query = clean_keywords(testo)
        web_context = cerca_web(query)
        if web_context == "ERRORE_RICERCA":
            await message.reply_text("Non riesco a cercare ora. Riprova tra poco.")
            return
        reply = await ask_groq(chat_id, query, nome, web_context=web_context)
    else:
        reply = await ask_groq(chat_id, testo, nome)

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
