import os
import sqlite3
import logging
import time
import asyncio

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

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
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot_a.db").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
BRIDGE_URL = os.getenv("BOT_B_BRIDGE_URL", "").strip().rstrip("/")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_A is missing.")
if not WEBHOOK_BASE_URL:
    raise RuntimeError("WEBHOOK_BASE_URL is missing.")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing.")
if not BRIDGE_URL:
    raise RuntimeError("BOT_B_BRIDGE_URL is missing.")
if not BRIDGE_SECRET:
    raise RuntimeError("BRIDGE_SECRET is missing.")

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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS session_messages (
                session_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(session_id, message_id)
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


def save_message_id(session_id, message_id):
    if not session_id or not message_id:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_messages VALUES (?, ?, ?)",
            (session_id, int(message_id), int(time.time())),
        )
        conn.commit()


def get_session_message_ids(session_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT message_id FROM session_messages WHERE session_id=? ORDER BY message_id",
            (session_id,),
        ).fetchall()
        return [int(row["message_id"]) for row in rows]


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


async def bridge_post(payload):
    headers = {
        "X-Bridge-Secret": BRIDGE_SECRET,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(BRIDGE_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response


def start_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Start", callback_data="start_support")],
        ]
    )


def support_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Get Advice", callback_data="support")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    # Delete the user's /start command so it does not remain in the chat.
    try:
        await update.message.delete()
    except Exception as exc:
        logger.warning(
            "Could not delete /start message_id=%s: %s",
            update.message.message_id,
            exc,
        )

    existing_session = get_session(update.effective_user.id)

    sent = await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=(
            f"👋 Hello {update.effective_user.first_name or 'there'}!\n\n"
            "Welcome. Press Start to continue."
        ),
        reply_markup=support_menu() if existing_session else start_menu(),
    )

    # An existing open session owns this menu message so Close can remove it.
    if existing_session:
        save_message_id(existing_session["session_id"], sent.message_id)


async def start_support_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return

    await query.answer("Ready.")

    await query.edit_message_text(
        f"👋 Hello {query.from_user.first_name or 'there'}!\n\n"
        "How can we help you?",
        reply_markup=support_menu(),
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
    save_message_id(session_id, query.message.message_id)

    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Unknown"
    username = f"@{user.username}" if user.username else "no username"

    payload = {
        "type": "BRIDGE_REQUEST",
        "session_id": session_id,
        "user_id": user.id,
        "name": name,
        "username": username,
        "message": "Customer pressed Get Advice.",
    }

    try:
        await bridge_post(payload)
        logger.info("Support request delivered to Bot B bridge. session=%s", session_id)
        sent = await query.message.reply_text(
            "✅ Support request sent.\n\n"
            "A team member will reply here shortly."
        )
        save_message_id(session_id, sent.message_id)
    except Exception:
        logger.exception("Could not send request to Bot B bridge.")
        sent = await query.message.reply_text(
            "❌ Support is temporarily unavailable. Please try again later."
        )
        save_message_id(session_id, sent.message_id)


async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        not update.message
        or not update.effective_user
        or update.effective_user.is_bot
    ):
        return

    user = update.effective_user
    session = get_session(user.id)

    if session:
        session_id = session["session_id"]
        save_message_id(session_id, update.message.message_id)

    if not session:
        session_id = create_session(user.id)
        save_message_id(session_id, update.message.message_id)
        name = " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Unknown"
        username = f"@{user.username}" if user.username else "no username"

        try:
            await bridge_post({
                "type": "BRIDGE_REQUEST",
                "session_id": session_id,
                "user_id": user.id,
                "name": name,
                "username": username,
                "message": "Customer sent a message without pressing the support button.",
            })
        except Exception:
            logger.exception("Could not create support request.")
            sent = await update.message.reply_text("❌ Support is temporarily unavailable.")
            save_message_id(session_id, sent.message_id)
            return
    else:
        session_id = session["session_id"]

    text = update.message.text or update.message.caption or ""
    if not text:
        sent = await update.message.reply_text("Please send your message as text for now.")
        save_message_id(session_id, sent.message_id)
        return

    touch_session(session_id)

    try:
        await bridge_post({
            "type": "BRIDGE_USER",
            "session_id": session_id,
            "user_id": user.id,
            "message": text[:3500],
        })
    except Exception:
        logger.exception("Could not relay user message.")
        sent = await update.message.reply_text(
            "❌ Your message could not be delivered. Please try again."
        )
        save_message_id(session_id, sent.message_id)


async def bridge_inbound(request: Request):
    if request.headers.get("X-Bridge-Secret", "") != BRIDGE_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    message_type = str(data.get("type", "")).strip()
    session_id = str(data.get("session_id", "")).strip()

    if message_type in ("BRIDGE_REPLY", "BRIDGE_ACK"):
        if not session_id:
            return PlainTextResponse("Missing session_id", status_code=400)

        body = str(data.get("message", "")).strip()
        if not body:
            return PlainTextResponse("Missing message", status_code=400)

        user_id = user_for_session(session_id)
        if not user_id:
            logger.warning("Unknown session from Bot B: %s", session_id)
            return PlainTextResponse("Unknown session", status_code=404)

        sent = await application.bot.send_message(chat_id=user_id, text=body[:4096])
        save_message_id(session_id, sent.message_id)
        touch_session(session_id)
        logger.info(
            "Delivered %s to customer. session=%s message_id=%s",
            message_type,
            session_id,
            sent.message_id,
        )
        return PlainTextResponse("OK")

    if message_type == "BRIDGE_CLOSE":
        if not session_id:
            return PlainTextResponse("Missing session_id", status_code=400)

        user_id = user_for_session(session_id)
        if user_id:
            message_ids = get_session_message_ids(session_id)

            # Delete one-by-one for maximum compatibility and detailed error
            # reporting. Telegram permits bots to delete incoming and outgoing
            # messages in private chats, subject to the 48-hour limit.
            deleted = 0
            failed = 0

            for message_id in message_ids:
                try:
                    await application.bot.delete_message(
                        chat_id=user_id,
                        message_id=message_id,
                    )
                    deleted += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Delete failed. session=%s message_id=%s error=%s",
                        session_id,
                        message_id,
                        exc,
                    )

            close_session(session_id)
            with db() as conn:
                conn.execute(
                    "DELETE FROM session_messages WHERE session_id=?",
                    (session_id,),
                )
                conn.commit()

            logger.info(
                "Conversation cleanup finished. session=%s tracked=%s deleted=%s failed=%s",
                session_id,
                len(message_ids),
                deleted,
                failed,
            )
        return PlainTextResponse("OK")

    return PlainTextResponse("Unknown bridge message", status_code=400)


async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    try:
        data = await request.json()
        update = Update.de_json(data=data, bot=application.bot)
        await application.update_queue.put(update)
    except Exception:
        logger.exception("Invalid Telegram webhook update.")
        return PlainTextResponse("Bad Request", status_code=400)

    return Response(status_code=200)


async def health(request: Request):
    return PlainTextResponse("Bot A is running.")


async def post_init(app: Application):
    me = await app.bot.get_me()
    logger.info("BOT A identity verified: @%s (id=%s)", me.username, me.id)
    logger.info("BOT A bridge endpoint configured.")
    await app.bot.set_webhook(
        url=f"{WEBHOOK_BASE_URL}/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        max_connections=40,
    )
    webhook = await app.bot.get_webhook_info()
    logger.info(
        "BOT A webhook active: url=%s pending=%s",
        webhook.url or "<empty>",
        webhook.pending_update_count,
    )


def build_web_app():
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/bridge/inbound", bridge_inbound, methods=["POST"]),
            Route(f"/{WEBHOOK_SECRET}", telegram_webhook, methods=["POST"]),
        ]
    )


async def main():
    global application
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        CallbackQueryHandler(start_support_button, pattern="^start_support$")
    )
    application.add_handler(CallbackQueryHandler(support_button, pattern="^support$"))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, user_message),
        group=1,
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

    logger.info("BOT A starting in CUSTOM WEBHOOK mode.")
    async with application:
        await application.start()
        await server.serve()
        await application.stop()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "/start - Open support menu\n\n"
            "Send a message anytime after opening support."
        )


if __name__ == "__main__":
    asyncio.run(main())
