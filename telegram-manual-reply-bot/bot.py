import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/support_bot.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from exc

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("manual-support-bot")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    folder = os.path.dirname(DATABASE_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_map (
                admin_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                telegram_message_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)"
        )
        conn.commit()


def upsert_user(user):
    with get_db() as conn:
        exists = conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (user.id,),
        ).fetchone()

        if exists:
            conn.execute("""
                UPDATE users
                SET first_name = ?, last_name = ?, username = ?, last_seen = ?
                WHERE telegram_id = ?
            """, (
                user.first_name or "",
                user.last_name or "",
                user.username or "",
                now_iso(),
                user.id,
            ))
        else:
            conn.execute("""
                INSERT INTO users
                (telegram_id, first_name, last_name, username, blocked, created_at, last_seen)
                VALUES (?, ?, ?, ?, 0, ?, ?)
            """, (
                user.id,
                user.first_name or "",
                user.last_name or "",
                user.username or "",
                now_iso(),
                now_iso(),
            ))
        conn.commit()


def is_blocked(user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT blocked FROM users WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row["blocked"])


def set_blocked(user_id, value):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET blocked = ? WHERE telegram_id = ?",
            (1 if value else 0, user_id),
        )
        conn.commit()


def save_message(user_id, direction, message_type, text="", telegram_message_id=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO messages
            (user_id, direction, message_type, text, telegram_message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            direction,
            message_type,
            text[:10000],
            telegram_message_id,
            now_iso(),
        ))
        conn.commit()


def map_admin_message(admin_message_id, user_id):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO message_map
            (admin_message_id, user_id, created_at)
            VALUES (?, ?, ?)
        """, (admin_message_id, user_id, now_iso()))
        conn.commit()


def get_user_for_admin_message(admin_message_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id FROM message_map WHERE admin_message_id = ?",
            (admin_message_id,),
        ).fetchone()
        return int(row["user_id"]) if row else None


def user_display(user):
    full_name = " ".join(
        p for p in [user.first_name, user.last_name] if p
    ).strip() or "Unknown"
    username = f"@{user.username}" if user.username else "no username"
    return f"{full_name} | {username} | ID: {user.id}"


def message_type(message):
    if message.text:
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "animation"
    if message.contact:
        return "contact"
    if message.location:
        return "location"
    return "other"


def message_text(message):
    return message.text or message.caption or ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    upsert_user(user)

    if is_blocked(user.id):
        await update.message.reply_text("This chat is currently unavailable.")
        return

    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}!\n\n"
        "Welcome to our support chat.\n\n"
        "Send your message here. A team member will read it and reply manually."
    )


async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    if user.id == ADMIN_ID:
        return

    upsert_user(user)

    if is_blocked(user.id):
        await update.message.reply_text("This chat is currently unavailable.")
        return

    msg = update.message
    mtype = message_type(msg)
    text = message_text(msg)

    save_message(user.id, "user", mtype, text, msg.message_id)

    header = (
        "📩 <b>NEW USER MESSAGE</b>\n\n"
        f"👤 <b>User:</b> {user_display(user)}\n"
        f"🕒 <b>Time:</b> {now_iso()}\n"
        f"📦 <b>Type:</b> {mtype}\n"
    )

    if text:
        header += f"\n💬 <b>Text:</b>\n{text[:3500]}"

    try:
        info = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=header,
            parse_mode=ParseMode.HTML,
        )
        map_admin_message(info.message_id, user.id)

        copied = await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=user.id,
            message_id=msg.message_id,
        )
        map_admin_message(copied.message_id, user.id)

        await update.message.reply_text(
            "✅ Your message has been received.\n\n"
            "A team member will reply here manually."
        )
    except Exception:
        logger.exception("Failed to deliver user message to admin.")
        await update.message.reply_text(
            "Sorry, your message could not be delivered right now. Please try again."
        )


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if update.effective_user.id != ADMIN_ID:
        return

    msg = update.message
    reply = msg.reply_to_message

    if not reply:
        await msg.reply_text(
            "↩️ Reply to the copied user message above to send a manual reply."
        )
        return

    user_id = get_user_for_admin_message(reply.message_id)
    if not user_id:
        await msg.reply_text("❌ I couldn't identify the user for this message.")
        return

    if is_blocked(user_id):
        await msg.reply_text("🚫 This user is currently blocked.")
        return

    try:
        sent = await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=ADMIN_ID,
            message_id=msg.message_id,
        )

        save_message(
            user_id,
            "admin",
            message_type(msg),
            message_text(msg),
            sent.message_id,
        )

        await msg.reply_text("✅ Reply sent.")
    except Exception:
        logger.exception("Failed to send admin reply.")
        await msg.reply_text("❌ Failed to send the reply to the user.")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    with get_db() as conn:
        rows = conn.execute("""
            SELECT telegram_id, first_name, last_name, username, blocked, last_seen
            FROM users
            ORDER BY last_seen DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        await update.message.reply_text("No users yet.")
        return

    lines = ["👥 <b>RECENT USERS</b>\n"]
    for row in rows:
        name = " ".join(
            x for x in [row["first_name"], row["last_name"]] if x
        ) or "Unknown"
        username = f"@{row['username']}" if row["username"] else "no username"
        status = "🚫 blocked" if row["blocked"] else "🟢 active"
        lines.append(
            f"• <b>{name}</b>\n"
            f"  {username}\n"
            f"  ID: <code>{row['telegram_id']}</code>\n"
            f"  {status}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /block USER_ID")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID must be numeric.")
        return

    set_blocked(user_id, True)
    await update.message.reply_text(f"🚫 User {user_id} blocked.")


async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock USER_ID")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID must be numeric.")
        return

    set_blocked(user_id, False)
    await update.message.reply_text(f"✅ User {user_id} unblocked.")


async def error_handler(update, context):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("block", block_command))
    application.add_handler(CommandHandler("unblock", unblock_command))

    application.add_handler(
        MessageHandler(
            filters.ALL & filters.User(user_id=ADMIN_ID),
            admin_message,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.User(user_id=ADMIN_ID),
            user_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Manual support bot is running.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
