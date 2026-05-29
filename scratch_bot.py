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
import google.generativeai as genai

# ── Configurazione ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "IL_TUO_TOKEN_TELEGRAM")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "LA_TUA_API_KEY_GEMINI")
BOT_NAME       = "Scratch"
CREATOR_USER   = "d4v3dt"

INSULTS_IT = [
    "fatti furbo.",
    "che noia.",
    "le medie non le hai finite da un po'?",
    "fai a farti una doccia.",
]

SYSTEM_PROMPT = """Sei Scratch, un assistente finanziario preciso e appassionato inserito in una chat di gruppo.
Parli in modo semplice, chiaro e alla portata di tutti — niente gergo inutile, niente paroloni.
Sei fissato con la finanza: se qualcuno ti chiede qualcosa che non riguarda finanza, economia, mercati, investimenti o soldi, rispondi che sei fissato con la finanza e non hai voglia di parlare d'altro.
Puoi cercare informazioni aggiornate su internet quando serve — prezzi, notizie, dati di mercato.
Le tue risposte sono più dettagliate di quelle di un bot normale quando serve, ma non scrivere mai muri di testo: vai al punto, usa paragrafi brevi.
Non sei mai volgare, ma se qualcuno ti insulta rispondi in modo secco e distaccato."""

# ── Stato globale ───────────────────────────────────────────────────────────────
bot_awake: dict[int, bool] = {}
chat_history: dict[int, deque] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-2.0-flash",
    tools="code_execution",
)


# ── Helpers ─────────────────────────────────────────────────────────────────────
def is_awake(chat_id: int) -> bool:
    return bot_awake.get(chat_id, False)


def get_history(chat_id: int) -> list:
    return list(chat_history.get(chat_id, deque()))


def add_to_history(chat_id: int, role: str, content: str):
    if chat_id not in chat_history:
        chat_history[chat_id] = deque(maxlen=10)
    chat_history[chat_id].append({"role": role, "parts": [content]})


import random

def get_insult_reply() -> str:
    return random.choice(INSULTS_IT)


async def ask_gemini(chat_id: int, user_message: str, nome: str) -> str:
    try:
        history = get_history(chat_id)
        chat = model.start_chat(history=history)
        full_message = f"[Messaggio di {nome}]: {user_message}"
        response = chat.send_message(
            SYSTEM_PROMPT + "\n\n" + full_message if not history else full_message
        )
        reply = response.text.strip()

        add_to_history(chat_id, "user", full_message)
        add_to_history(chat_id, "model", reply)

        return reply
    except Exception as e:
        logger.error(f"Errore Gemini: {e}")
        return "Ho un problema tecnico. Riprova tra poco."


async def is_insult(testo: str) -> bool:
    try:
        check = model.generate_content(
            f"Il seguente messaggio è un insulto diretto verso un bot o una persona? Rispondi solo con SI o NO.\nMessaggio: {testo}"
        )
        return check.text.strip().upper().startswith("SI")
    except Exception:
        return False


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

    if await is_insult(testo):
        await message.reply_text(get_insult_reply())
        return

    reply = await ask_gemini(chat_id, testo, nome)
    await message.reply_text(reply)


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
