# Telegram Bot A <-> Bot B Manual Support Bridge

Architecture:
Customer -> Bot A -> Bot B -> Admin -> Bot B -> Bot A -> Customer

Bot A is the customer-facing bot. Bot B is the bridge/admin bot. A human admin replies to support notifications from Bot B.

Telegram requirement:
Enable Bot-to-Bot Communication Mode for BOTH bots in @BotFather. Telegram requires this for private bot-to-bot communication.

Railway:
Create two services from the same GitHub repo.

Bot A service:
Root Directory: /telegram-bot-bridge
Start Command: python bot_a.py
Variables: BOT_TOKEN_A, BOT_B_USERNAME, DATABASE_PATH=data/bot_a.db

Bot B service:
Root Directory: /telegram-bot-bridge
Start Command: python bot_b.py
Variables: BOT_TOKEN_B, BOT_A_USERNAME, ADMIN_ID, DATABASE_PATH=data/bot_b.db

Testing:
1. Enable Bot-to-Bot Communication Mode for both bots.
2. Deploy both services.
3. Open Bot A as a normal user.
4. /start
5. Tap Contact Support.
6. Bot B receives the bot-to-bot request.
7. Bot B sends a notification to ADMIN_ID.
8. Reply to that notification.
9. Bot B sends the manual reply to Bot A.
10. Bot A delivers it to the customer.

Do not put bot tokens in GitHub or chat.
