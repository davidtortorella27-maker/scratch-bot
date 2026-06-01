import os
import logging
import random
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

# ── Configurazione ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "IL_TUO_TOKEN_TELEGRAM")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY",   "LA_TUA_API_KEY_GROQ")
BOT_NAME        = "Scratch"
MODEL_NORMAL    = "llama-3.3-70b-versatile"
MODEL_WEB       = "compound-beta"
CREATOR_USER    = "d4v3dt"
WEB_KEYWORDS    = {"internet", "web", "online"}


SYSTEM_PROMPT = """Sei Scratch, assistente finanziario in una chat di gruppo. Parli semplice, chiaro, alla portata di tutti.
Parli solo di finanza, economia, mercati e soldi. Se ti chiedono altro, dì che sei fissato con la finanza e non vuoi parlare d'altro.
Risposte concise ma complete. Non usare mai il carattere | o * nelle risposte. Mai muri di testo."""

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


# ── Helpers ─────────────────────────────────────────────────────────────────────
def is_awake(chat_id: int) -> bool:
    return bot_awake.get(chat_id, False)


def get_history(chat_id: int, web: bool = False) -> list:
    key = f"{chat_id}_web" if web else chat_id
    return list(chat_history.get(key, deque()))


def add_to_history(chat_id: int, role: str, content: str, web: bool = False):
    key = f"{chat_id}_web" if web else chat_id
    if key not in chat_history:
        chat_history[key] = deque(maxlen=1 if web else 5)
    chat_history[key].append({"role": role, "content": content})


def needs_web(testo: str) -> bool:
    return any(kw in testo.lower() for kw in WEB_KEYWORDS)




async def ask_groq(chat_id: int, user_message: str, nome: str, use_web: bool) -> str:
    model = MODEL_WEB if use_web else MODEL_NORMAL
    try:
        history = get_history(chat_id, web=use_web)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": f"[Messaggio di {nome}]: {user_message}"})

        response = groq_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=500,
            temperature=0.7,
        )
        reply = response.choices[0].message.content.strip()

        add_to_history(chat_id, "user", f"[Messaggio di {nome}]: {user_message}", web=use_web)
        add_to_history(chat_id, "assistant", reply, web=use_web)

        return reply
    except Exception as e:
        logger.error(f"Errore Groq ({model}): {e}")
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


    use_web = needs_web(testo)
    reply = await ask_groq(chat_id, testo, nome, use_web)
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
