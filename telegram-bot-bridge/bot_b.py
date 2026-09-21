import os
import sqlite3
import logging
import time
import asyncio

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

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
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot_b.db").strip()
BRIDGE_URL = os.getenv("BOT_A_BRIDGE_URL", "").strip().rstrip("/")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_B is missing.")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing.")
if not BRIDGE_URL:
    raise RuntimeError("BOT_A_BRIDGE_URL is missing.")
if not BRIDGE_SECRET:
    raise RuntimeError("BRIDGE_SECRET is missing.")

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


async def bridge_post(payload):
    headers = {
        "X-Bridge-Secret": BRIDGE_SECRET,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(BRIDGE_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response


def close_markup(session_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🔴 Close conversation",
                callback_data=f"close:{session_id}",
            )
        ]]
    )


async def notify_admin(bot, text, session_id):
    sent = await bot.send_message(
        chat_id=ADMIN_ID,
        text=text + "\n\n↩️ Reply to THIS message to send your manual reply.",
        reply_markup=close_markup(session_id),
    )
    save_reply_target(sent.message_id, session_id)
    logger.info(
        "Admin notification sent. session=%s message_id=%s",
        session_id,
        sent.message_id,
    )


async def bridge_inbound(request: Request):
    if request.headers.get("X-Bridge-Secret", "") != BRIDGE_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    message_type = str(data.get("type", "")).strip()
    session_id = str(data.get("session_id", "")).strip()

    if message_type == "BRIDGE_REQUEST":
        if not session_id:
            return PlainTextResponse("Missing session_id", status_code=400)

        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            return PlainTextResponse("Invalid user_id", status_code=400)

        name = str(data.get("name", "Unknown")).strip() or "Unknown"
        username = str(data.get("username", "no username")).strip() or "no username"
        event = str(data.get("message", "")).strip()

        save_session(session_id, user_id, name, username)

        await notify_admin(
            application.bot,
            "📩 NEW SUPPORT REQUEST\n\n"
            f"🆔 Session: {session_id}\n"
            f"👤 Name: {name}\n"
            f"🔗 Username: {username}\n"
            f"🪪 User ID: {user_id}\n\n"
            f"💬 {event}",
            session_id,
        )

        try:
            await bridge_post({
                "type": "BRIDGE_ACK",
                "session_id": session_id,
                "message": (
                    "✅ Your support request has reached our team. "
                    "Please wait for a manual reply."
                ),
            })
            logger.info("ACK delivered to Bot A. session=%s", session_id)
        except Exception:
            logger.exception("ACK to Bot A bridge failed.")

        return PlainTextResponse("OK")

    if message_type == "BRIDGE_USER":
        if not session_id:
            return PlainTextResponse("Missing session_id", status_code=400)

        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            return PlainTextResponse("Invalid user_id", status_code=400)

        message_text = str(data.get("message", "")).strip()
        save_session(session_id, user_id)

        await notify_admin(
            application.bot,
            "💬 USER MESSAGE\n\n"
            f"🆔 Session: {session_id}\n"
            f"🪪 User ID: {user_id}\n\n"
            f"💬 {message_text}",
            session_id,
        )
        return PlainTextResponse("OK")

    if message_type == "BRIDGE_HEALTH":
        return PlainTextResponse("OK")

    return PlainTextResponse("Unknown bridge message", status_code=400)


async def health(request: Request):
    return PlainTextResponse("Bot B is running.")


async def post_init(app: Application):
    me = await app.bot.get_me()
    logger.info("BOT B identity verified: @%s (id=%s)", me.username, me.id)

    # Bot B receives admin messages through long polling.
    # Remove any old outgoing webhook first because Telegram does not allow
    # getUpdates while a webhook is active.
    webhook = await app.bot.get_webhook_info()
    if webhook.url:
        await app.bot.delete_webhook(drop_pending_updates=False)
        logger.info("Old Telegram webhook removed. pending=%s", webhook.pending_update_count)
    else:
        logger.info("No Telegram webhook was configured for Bot B.")


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.from_user:
        return

    logger.info(
        "Incoming admin message. user_id=%s chat_id=%s message_id=%s reply_to=%s",
        message.from_user.id,
        message.chat_id,
        message.message_id,
        message.reply_to_message.message_id if message.reply_to_message else None,
    )

    if message.from_user.id != ADMIN_ID:
        logger.warning(
            "Ignoring message from non-admin user_id=%s",
            message.from_user.id,
        )
        return

    if not message.reply_to_message:
        await message.reply_text("↩️ Reply directly to a support notification.")
        return

    session_id = get_session_for_admin_message(message.reply_to_message.message_id)

    if not session_id:
        await message.reply_text(
            "❌ That message is not linked to an active support session."
        )
        return

    text = message.text or message.caption or ""
    if not text:
        await message.reply_text("Please send a text reply for now.")
        return

    try:
        await bridge_post({
            "type": "BRIDGE_REPLY",
            "session_id": session_id,
            "message": text[:3500],
        })
        await message.reply_text("✅ Reply sent → Bot A → customer.")
        logger.info("Manual reply delivered to Bot A. session=%s", session_id)
    except Exception:
        logger.exception("Could not send reply to Bot A bridge.")
        await message.reply_text("❌ Reply delivery failed. Please try again.")


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
        await bridge_post({
            "type": "BRIDGE_CLOSE",
            "session_id": session_id,
        })
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
            "4. The response goes Bot B → HTTPS → Bot A → customer.\n\n"
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


def build_web_app():
    # HTTPS bridge remains available for Bot A <-> Bot B communication.
    # Telegram admin updates no longer depend on this custom webhook.
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/bridge/inbound", bridge_inbound, methods=["POST"]),
        ]
    )


async def main():
    global application
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("sessions", sessions_command))
    application.add_handler(
        CallbackQueryHandler(close_callback, pattern=r"^close:")
    )
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, admin_reply),
        group=0,
    )

    web_app = build_web_app()
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    logger.info("BOT B starting in POLLING + HTTPS BRIDGE mode.")
    logger.info("BOT B admin updates will use Telegram long polling.")
    logger.info("BOT B HTTPS bridge endpoint configured.")

    async with application:
        await application.start()
        if application.updater is None:
            raise RuntimeError("PTB updater is unexpectedly unavailable.")
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        try:
            await server.serve()
        finally:
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
