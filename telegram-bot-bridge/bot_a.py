import os
import sqlite3
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN_A", "").strip()
BOT_B_USERNAME = os.getenv("BOT_B_USERNAME", "").strip().lstrip("@")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot_a.db").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_A is missing.")
if not BOT_B_USERNAME:
    raise RuntimeError("BOT_B_USERNAME is missing.")
if not WEBHOOK_BASE_URL:
    raise RuntimeError("WEBHOOK_BASE_URL is missing.")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bot-a")


def db():
    folder = os.path.dirname(DATABASE_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        conn.commit()


def get_session(user_id):
    with db() as conn:
        return conn.execute(
            """SELECT * FROM sessions
               WHERE user_id=? AND status='open'
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()


def create_session(user_id):
    session_id = f"S{user_id}-{int(time.time())}"
    now = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, 'open', ?, ?)",
            (session_id, user_id, now, now),
        )
        conn.commit()
    return session_id


def touch_session(session_id):
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE session_id=?",
            (int(time.time()), session_id),
        )
        conn.commit()


def close_session(session_id):
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET status='closed', updated_at=? WHERE session_id=?",
            (int(time.time()), session_id),
        )
        conn.commit()


def user_for_session(session_id):
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row["user_id"]) if row else None


async def send_to_b(context, text):
    logger.info("Sending bridge message to @%s", BOT_B_USERNAME)
    return await context.bot.send_message(
        chat_id=f"@{BOT_B_USERNAME}",
        text=text[:4096],
    )


def menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Contact Support", callback_data="support")],
            [InlineKeyboardButton("🔄 New Support Request", callback_data="support")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name or 'there'}!\n\n"
        "Welcome. If you need help, tap the button below.\n\n"
        "A support team member will reply manually.",
        reply_markup=menu(),
    )


async def support_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return

    await query.answer("Connecting you to support...")

    user = query.from_user
    session = get_session(user.id)
    session_id = session["session_id"] if session else create_session(user.id)
    touch_session(session_id)

    name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    ).strip() or "Unknown"
    username = f"@{user.username}" if user.username else "no username"

    request = (
        "BRIDGE_REQUEST\n"
        f"SESSION:{session_id}\n"
        f"USER_ID:{user.id}\n"
        f"NAME:{name}\n"
        f"USERNAME:{username}\n"
        "MESSAGE:Customer pressed Contact Support."
    )

    try:
        sent = await send_to_b(context, request)
        logger.info(
            "Support request delivered to Bot B. message_id=%s",
            getattr(sent, "message_id", "unknown"),
        )
        await query.message.reply_text(
            "✅ Support request sent.\n\n"
            "A team member will reply here shortly."
        )
    except Exception:
        logger.exception("Could not send request to Bot B.")
        await query.message.reply_text(
            "❌ Support is temporarily unavailable. Please try again later."
        )


async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        not update.message
        or not update.effective_user
        or update.effective_user.is_bot
    ):
        return

    user = update.effective_user
    session = get_session(user.id)

    if not session:
        session_id = create_session(user.id)
        name = " ".join(
            x for x in [user.first_name, user.last_name] if x
        ).strip() or "Unknown"
        username = f"@{user.username}" if user.username else "no username"

        try:
            await send_to_b(
                context,
                "BRIDGE_REQUEST\n"
                f"SESSION:{session_id}\n"
                f"USER_ID:{user.id}\n"
                f"NAME:{name}\n"
                f"USERNAME:{username}\n"
                "MESSAGE:Customer sent a message without pressing the support button.",
            )
        except Exception:
            logger.exception("Could not create support request.")
            await update.message.reply_text(
                "❌ Support is temporarily unavailable."
            )
            return
    else:
        session_id = session["session_id"]

    text = update.message.text or update.message.caption or ""
    if not text:
        await update.message.reply_text(
            "Please send your message as text for now."
        )
        return

    touch_session(session_id)

    try:
        await send_to_b(
            context,
            "BRIDGE_USER\n"
            f"SESSION:{session_id}\n"
            f"USER_ID:{user.id}\n"
            f"MESSAGE:{text[:3500]}",
        )
    except Exception:
        logger.exception("Could not relay user message.")
        await update.message.reply_text(
            "❌ Your message could not be delivered. Please try again."
        )


async def bot_b_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    sender_username = (message.from_user.username or "").lower()
    if not message.from_user.is_bot:
        return
    if sender_username != BOT_B_USERNAME.lower():
        return

    text = message.text or ""
    if not text.startswith("BRIDGE_"):
        return

    logger.info("Received bridge message from @%s: %s", BOT_B_USERNAME, text[:160])

    parts = text.split("\n", 1)
    command = parts[0]

    if command in ("BRIDGE_REPLY", "BRIDGE_ACK") and len(parts) == 2:
        lines = parts[1].split("\n", 1)
        if len(lines) != 2 or not lines[0].startswith("SESSION:"):
            logger.warning("Invalid bridge reply format.")
            return

        session_id = lines[0].split(":", 1)[1].strip()
        body = lines[1].removeprefix("MESSAGE:")
        user_id = user_for_session(session_id)

        if not user_id:
            logger.warning("Unknown session: %s", session_id)
            return

        await context.bot.send_message(chat_id=user_id, text=body[:4096])
        touch_session(session_id)
        return

    if command == "BRIDGE_CLOSE":
        session_id = parts[1].strip() if len(parts) == 2 else ""
        user_id = user_for_session(session_id)

        if user_id:
            close_session(session_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ This support conversation has been closed.\n\n"
                    "Tap Contact Support if you need help again."
                ),
                reply_markup=menu(),
            )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "/start - Open support menu\n\n"
            "Send a message anytime after opening support."
        )


async def post_init(app: Application):
    me = await app.bot.get_me()
    webhook = await app.bot.get_webhook_info()
    logger.info(
        "BOT A identity verified: @%s (id=%s)",
        me.username,
        me.id,
    )
    logger.info(
        "Existing webhook before startup: url=%s pending=%s",
        webhook.url or "<empty>",
        webhook.pending_update_count,
    )


def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(support_button, pattern="^support$"))

    # Bot-to-bot messages from Bot B
    app.add_handler(MessageHandler(filters.ALL, bot_b_message), group=0)

    # Normal customer messages
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, user_message),
        group=1,
    )

    webhook_url = f"{WEBHOOK_BASE_URL}/{WEBHOOK_SECRET}"

    logger.info("BOT A starting in WEBHOOK mode.")
    logger.info("BOT A bridge target: @%s", BOT_B_USERNAME)
    logger.info("BOT A public webhook: %s", WEBHOOK_BASE_URL)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_SECRET,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        max_connections=40,
    )


if __name__ == "__main__":
    main()
