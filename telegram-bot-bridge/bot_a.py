import os
import sqlite3
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN_A", "").strip()
BOT_B_USERNAME = os.getenv("BOT_B_USERNAME", "").strip().lstrip("@")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot_a.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_A is missing.")
if not BOT_B_USERNAME:
    raise RuntimeError("BOT_B_USERNAME is missing.")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
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
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        conn.commit()

def get_session(user_id):
    with db() as conn:
        return conn.execute("""SELECT * FROM sessions
            WHERE user_id=? AND status='open'
            ORDER BY updated_at DESC LIMIT 1""", (user_id,)).fetchone()

def create_session(user_id):
    session_id = f"S{user_id}-{int(time.time())}"
    now = int(time.time())
    with db() as conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, 'open', ?, ?)",
                     (session_id, user_id, now, now))
        conn.commit()
    return session_id

def touch_session(session_id):
    with db() as conn:
        conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?",
                     (int(time.time()), session_id))
        conn.commit()

def close_session(session_id):
    with db() as conn:
        conn.execute("UPDATE sessions SET status='closed', updated_at=? WHERE session_id=?",
                     (int(time.time()), session_id))
        conn.commit()

def user_for_session(session_id):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE session_id=?",
                           (session_id,)).fetchone()
        return int(row["user_id"]) if row else None

async def send_to_b(context, text):
    return await context.bot.send_message(chat_id=f"@{BOT_B_USERNAME}", text=text)

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contact Support", callback_data="support")],
        [InlineKeyboardButton("🔄 New Support Request", callback_data="support")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name or 'there'}!\n\n"
        "Welcome. If you need help, tap the button below.\n\n"
        "A support team member will reply manually.",
        reply_markup=menu()
    )

async def support_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer("Connecting you to support...")
    user = query.from_user
    session = get_session(user.id)
    session_id = session["session_id"] if session else create_session(user.id)
    touch_session(session_id)

    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Unknown"
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
        await send_to_b(context, request)
        await query.message.reply_text("✅ Support request sent.\n\nA team member will reply here shortly.")
    except Exception:
        logger.exception("Could not send request to Bot B.")
        await query.message.reply_text("❌ Support is temporarily unavailable. Please try again later.")

async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return
    user = update.effective_user
    session = get_session(user.id)
    if not session:
        session_id = create_session(user.id)
        name = " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Unknown"
        username = f"@{user.username}" if user.username else "no username"
        try:
            await send_to_b(context,
                "BRIDGE_REQUEST\n"
                f"SESSION:{session_id}\nUSER_ID:{user.id}\nNAME:{name}\nUSERNAME:{username}\n"
                "MESSAGE:Customer sent a message without pressing the support button."
            )
        except Exception:
            logger.exception("Could not create support request.")
            await update.message.reply_text("❌ Support is temporarily unavailable.")
            return
    else:
        session_id = session["session_id"]

    touch_session(session_id)
    text = update.message.text or update.message.caption or ""
    if not text:
        await update.message.reply_text("Please send your message as text for now.")
        return
    try:
        await send_to_b(context,
            "BRIDGE_USER\n"
            f"SESSION:{session_id}\nUSER_ID:{user.id}\nMESSAGE:{text[:3500]}"
        )
    except Exception:
        logger.exception("Could not relay user message.")
        await update.message.reply_text("❌ Your message could not be delivered. Please try again.")

async def bot_b_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user or not message.from_user.is_bot:
        return
    if (message.from_user.username or "").lower() != BOT_B_USERNAME.lower():
        return
    text = message.text or ""
    if not text.startswith("BRIDGE_"):
        return
    parts = text.split("\n", 1)
    command = parts[0]
    if command in ("BRIDGE_REPLY", "BRIDGE_ACK") and len(parts) == 2:
        lines = parts[1].split("\n", 1)
        if len(lines) != 2 or not lines[0].startswith("SESSION:"):
            return
        session_id = lines[0].split(":", 1)[1].strip()
        body = lines[1].removeprefix("MESSAGE:")
        user_id = user_for_session(session_id)
        if not user_id:
            return
        await context.bot.send_message(chat_id=user_id, text=body[:4096])
        touch_session(session_id)
    elif command == "BRIDGE_CLOSE":
        session_id = parts[1].strip() if len(parts) == 2 else ""
        user_id = user_for_session(session_id)
        if user_id:
            close_session(session_id)
            await context.bot.send_message(chat_id=user_id, text="✅ This support conversation has been closed.\n\nTap Contact Support if you need help again.", reply_markup=menu())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("/start - Open support menu\n\nSend a message anytime after opening support.")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(support_button, pattern="^support$"))
    app.add_handler(MessageHandler(filters.ALL, bot_b_message), group=0)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, user_message), group=1)
    logger.info("BOT A is running. Bridge target: @%s", BOT_B_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    main()
