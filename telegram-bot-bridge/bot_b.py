import os
import sqlite3
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN_B", "").strip()
BOT_A_USERNAME = os.getenv("BOT_A_USERNAME", "").strip().lstrip("@")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot_b.db").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_B is missing.")
if not BOT_A_USERNAME:
    raise RuntimeError("BOT_A_USERNAME is missing.")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing.")
if not WEBHOOK_BASE_URL:
    raise RuntimeError("WEBHOOK_BASE_URL is missing.")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be numeric.") from exc

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bot-b")


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
            """CREATE TABLE IF NOT EXISTS replies (
                admin_message_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                updated_at INTEGER NOT NULL
            )"""
        )
        conn.commit()


def save_session(session_id, user_id, name="", username=""):
    with db() as conn:
        conn.execute(
            """INSERT INTO sessions(
                session_id,user_id,name,username,status,updated_at
            )
            VALUES(?,?,?,?, 'open', ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id=excluded.user_id,
                name=CASE
                    WHEN excluded.name='' THEN sessions.name
                    ELSE excluded.name
                END,
                username=CASE
                    WHEN excluded.username='' THEN sessions.username
                    ELSE excluded.username
                END,
                status='open',
                updated_at=excluded.updated_at""",
            (session_id, user_id, name, username, int(time.time())),
        )
        conn.commit()


def save_reply_target(admin_message_id, session_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO replies VALUES (?, ?, ?)",
            (admin_message_id, session_id, int(time.time())),
        )
        conn.commit()


def get_session_for_admin_message(message_id):
    with db() as conn:
        row = conn.execute(
            "SELECT session_id FROM replies WHERE admin_message_id=?",
            (message_id,),
        ).fetchone()
        return row["session_id"] if row else None


def close_session_local(session_id):
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET status='closed' WHERE session_id=?",
            (session_id,),
        )
        conn.commit()


def get_open_sessions():
    with db() as conn:
        return conn.execute(
            """SELECT session_id,user_id,name,username
               FROM sessions
               WHERE status='open'
               ORDER BY updated_at DESC
               LIMIT 30"""
        ).fetchall()


async def send_to_bot_a(context, text):
    logger.info("Sending bridge message to @%s", BOT_A_USERNAME)
    return await context.bot.send_message(
        chat_id=f"@{BOT_A_USERNAME}",
        text=text[:4096],
    )


def close_markup(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔴 Close conversation",
                    callback_data=f"close:{session_id}",
                )
            ]
        ]
    )


async def notify_admin(context, text, session_id):
    sent = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            text
            + "\n\n↩️ Reply to THIS message to send your manual reply."
        ),
        reply_markup=close_markup(session_id),
    )
    save_reply_target(sent.message_id, session_id)
    logger.info(
        "Admin notification sent. session=%s message_id=%s",
        session_id,
        sent.message_id,
    )


async def bot_a_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    if not message.from_user.is_bot:
        return

    sender_username = (message.from_user.username or "").lower()
    if sender_username != BOT_A_USERNAME.lower():
        return

    text = message.text or ""
    if not text.startswith("BRIDGE_"):
        return

    logger.info(
        "Received bridge message from @%s: %s",
        BOT_A_USERNAME,
        text[:160],
    )

    lines = text.split("\n")
    command = lines[0]
    data = {}

    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()

    session_id = data.get("SESSION")
    user_id = data.get("USER_ID")

    if not session_id or not user_id:
        logger.warning("Bridge message missing session/user id.")
        return

    try:
        user_id_int = int(user_id)
    except ValueError:
        logger.warning("Invalid USER_ID in bridge message.")
        return

    if command == "BRIDGE_REQUEST":
        name = data.get("NAME", "Unknown")
        username = data.get("USERNAME", "no username")
        event = data.get("MESSAGE", "")

        save_session(session_id, user_id_int, name, username)

        await notify_admin(
            context,
            "📩 NEW SUPPORT REQUEST\n\n"
            f"🆔 Session: {session_id}\n"
            f"👤 Name: {name}\n"
            f"🔗 Username: {username}\n"
            f"🪪 User ID: {user_id_int}\n\n"
            f"💬 {event}",
            session_id,
        )

        try:
            await send_to_bot_a(
                context,
                "BRIDGE_ACK\n"
                f"SESSION:{session_id}\n"
                "MESSAGE:✅ Your support request has reached our team. "
                "Please wait for a manual reply.",
            )
        except Exception:
            logger.exception("ACK to Bot A failed.")

    elif command == "BRIDGE_USER":
        message_text = data.get("MESSAGE", "")
        save_session(session_id, user_id_int)

        await notify_admin(
            context,
            "💬 USER MESSAGE\n\n"
            f"🆔 Session: {session_id}\n"
            f"🪪 User ID: {user_id_int}\n\n"
            f"💬 {message_text}",
            session_id,
        )


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.from_user:
        return
    if message.from_user.id != ADMIN_ID:
        return

    if not message.reply_to_message:
        await message.reply_text(
            "↩️ Reply directly to a support notification."
        )
        return

    session_id = get_session_for_admin_message(
        message.reply_to_message.message_id
    )

    if not session_id:
        await message.reply_text(
            "❌ That message is not linked to an active support session."
        )
        return

    text = message.text or message.caption or ""
    if not text:
        await message.reply_text(
            "Please send a text reply for now."
        )
        return

    try:
        await send_to_bot_a(
            context,
            "BRIDGE_REPLY\n"
            f"SESSION:{session_id}\n"
            f"MESSAGE:{text[:3500]}",
        )
        await message.reply_text(
            "✅ Reply sent → Bot A → customer."
        )
    except Exception:
        logger.exception("Could not send reply to Bot A.")
        await message.reply_text(
            "❌ Reply failed. Check Bot-to-Bot Communication Mode "
            "on both bots."
        )


async def close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_ID:
        return
    if not query.data or not query.data.startswith("close:"):
        return

    session_id = query.data.split(":", 1)[1]
    await query.answer("Closing...")

    try:
        await send_to_bot_a(
            context,
            f"BRIDGE_CLOSE\n{session_id}",
        )
        close_session_local(session_id)

        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"🔴 Conversation {session_id} closed."
            )
    except Exception:
        logger.exception("Close failed.")
        if query.message:
            await query.message.reply_text(
                "❌ Could not close this conversation."
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_user
        and update.effective_user.id == ADMIN_ID
        and update.message
    ):
        await update.message.reply_text(
            "🛠 Support Admin Bot\n\n"
            "Reply to a support notification to message the customer.\n"
            "/sessions - open conversations\n"
            "/help - instructions"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_user
        and update.effective_user.id == ADMIN_ID
        and update.message
    ):
        await update.message.reply_text(
            "🛠 Admin help\n\n"
            "1. Wait for a support notification.\n"
            "2. Use Telegram Reply ↩️ on that notification.\n"
            "3. Type your manual response and send it.\n"
            "4. The response goes Bot B → Bot A → customer.\n\n"
            "/sessions - list open conversations"
        )


async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        not update.effective_user
        or update.effective_user.id != ADMIN_ID
        or not update.message
    ):
        return

    rows = get_open_sessions()

    if not rows:
        await update.message.reply_text("No open support sessions.")
        return

    lines = ["🟢 OPEN SUPPORT SESSIONS\n"]

    for row in rows:
        lines.append(
            f"• {row['session_id']}\n"
            f"  {row['name'] or 'Unknown'} | "
            f"{row['username'] or 'no username'}\n"
            f"  User ID: {row['user_id']}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def post_init(app: Application):
    me = await app.bot.get_me()
    webhook = await app.bot.get_webhook_info()

    logger.info(
        "BOT B identity verified: @%s (id=%s)",
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
    app.add_handler(CommandHandler("sessions", sessions_command))
    app.add_handler(
        CallbackQueryHandler(close_callback, pattern=r"^close:")
    )

    # Bot-to-bot messages from Bot A
    app.add_handler(MessageHandler(filters.ALL, bot_a_message), group=0)

    # Human admin replies
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, admin_reply),
        group=1,
    )

    webhook_url = f"{WEBHOOK_BASE_URL}/{WEBHOOK_SECRET}"

    logger.info("BOT B starting in WEBHOOK mode.")
    logger.info("BOT B bridge source: @%s", BOT_A_USERNAME)
    logger.info("BOT B public webhook: %s", WEBHOOK_BASE_URL)

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

