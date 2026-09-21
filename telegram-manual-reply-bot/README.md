# Telegram Manual Reply Bot

Manual Telegram support bot for one-to-one admin replies.

## Features

- /start welcome flow
- User text/media is delivered to the admin
- Admin replies using Telegram's normal Reply action
- Replies go back to the correct user
- Supports text, photos, videos, documents, audio, voice, stickers and other Telegram message types through copy_message
- SQLite user/message history
- /users
- /block USER_ID
- /unblock USER_ID
- Admin-only commands
- Telegram polling; no webhook required

## Railway

Environment variables:

- BOT_TOKEN = BotFather token
- ADMIN_ID = numeric Telegram user ID
- DATABASE_PATH = data/support_bot.db

Start command:

python bot.py

For persistent SQLite, attach persistent storage to the data directory, or migrate the database to PostgreSQL for production.

Never commit your real bot token.
