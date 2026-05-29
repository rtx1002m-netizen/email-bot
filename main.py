import logging
import asyncio
import aiohttp
import json
import os
import re
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

BOT_TOKEN = "8871669907:AAHx_XVEkY0KhsiJCkuab7XeW0qxC18oSUg"
DB_FILE = "database.json"
WAITING_USERNAME = 1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": {}, "usernames": {}}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(telegram_id):
    db = load_db()
    return db["users"].get(str(telegram_id))

def save_user(telegram_id, data):
    db = load_db()
    db["users"][str(telegram_id)] = data
    if data.get("username"):
        db["usernames"][data["username"]] = str(telegram_id)
    save_db(db)

def username_taken(username, exclude_id=None):
    db = load_db()
    owner = db["usernames"].get(username.lower())
    if owner and owner != str(exclude_id):
        return True
    return False

def delete_user(telegram_id):
    db = load_db()
    user = db["users"].get(str(telegram_id))
    if user and user.get("username"):
        db["usernames"].pop(user["username"], None)
    db["users"].pop(str(telegram_id), None)
    save_db(db)

MAILTM_API = "https://api.mail.tm"

async def create_mailtm_account():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{MAILTM_API}/domains") as r:
            domains = await r.json()
            domain = domains["hydra:member"][0]["domain"]
        import random, string
        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username}@{domain}"
        payload = {"address": address, "password": password}
        async with session.post(f"{MAILTM_API}/accounts", json=payload) as r:
            if r.status not in (200, 201):
                return None
            account = await r.json()
        async with session.post(f"{MAILTM_API}/token", json=payload) as r:
            if r.status != 200:
                return None
            token_data = await r.json()
        return {"address": address, "password": password, "account_id": account["id"], "token": token_data["token"]}

async def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{MAILTM_API}/messages", headers=headers) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return data.get("hydra:member", [])

async def get_message_content(token, msg_id):
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{MAILTM_API}/messages/{msg_id}", headers=headers) as r:
            if r.status != 200:
                return None
            return await r.json()

async def delete_mailtm_account(token, account_id):
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        await session.delete(f"{MAILTM_API}/accounts/{account_id}", headers=headers)

async def refresh_token(address, password):
    async with aiohttp.ClientSession() as session:
        payload = {"address": address, "password": password}
        async with session.post(f"{MAILTM_API}/token", json=payload) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data["token"]

def main_keyboard(emails):
    buttons = [[InlineKeyboardButton("+ اضافة ايميل جديد", callback_data="add_email")]]
    if emails:
        for i, em in enumerate(emails):
            buttons.append([
                InlineKeyboardButton(f"inbox {em['address']}", callback_data=f"view_inbox_{i}"),
                InlineKeyboardButton("حذف", callback_data=f"delete_email_{i}")
            ])
    buttons.append([InlineKeyboardButton("تسجيل الخروج", callback_data="logout")])
    return InlineKeyboardMarkup(buttons)

async def show_dashboard(update, context, user):
    text = f"مرحبا {user['username']}\n\nايميلاتك: {len(user.get('emails', []))}\n\naختر ما تريد:"
    kb = main_keyboard(user.get("emails", []))
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

async def start(update, context):
    telegram_id = str(update.effective_user.id)
    user = get_user(telegram_id)
    if user:
        await show_dashboard(update, context, user)
        return ConversationHandler.END
    await update.message.reply_text("اهلا! ادخل اسم مستخدم (احرف وارقام فقط، 3 احرف على الاقل):")
    return WAITING_USERNAME

async def receive_username(update, context):
    telegram_id = str(update.effective_user.id)
    username = update.message.text.strip().lower()
    if len(username) < 3:
        await update.message.reply_text("الاسم قصير! 3 احرف على الاقل.")
        return WAITING_USERNAME
    if not username.isalnum():
        await update.message.reply_text("احرف وارقام فقط بدون رموز.")
        return WAITING_USERNAME
    if username_taken(username, exclude_id=telegram_id):
        await update.message.reply_text(f"الاسم {username} مشغول! جرب اسما اخر:")
        return WAITING_USERNAME
    user = {"username": username, "telegram_id": telegram_id, "emails": [], "created_at": datetime.now().isoformat()}
    save_user(telegram_id, user)
    await update.message.reply_text(f"تم انشاء حسابك! اسم المستخدم: {username}")
    await show_dashboard(update, context, user)
    return ConversationHandler.END

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    telegram_id = str(update.effective_user.id)
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text("ارسل /start")
        return
    data = query.data
    if data == "add_email":
        if len(user.get("emails", [])) >= 5:
            await query.answer("الحد الاقصى 5 ايميلات!", show_alert=True)
            return
        await query.edit_message_text("جاري انشاء ايميل...")
        account = await create_mailtm_account()
        if not account:
            await query.edit_message_text("فشل الانشاء، حاول مرة اخرى.")
            return
        account["messages_seen"] = []
        user.setdefault("emails", []).append(account)
        save_user(telegram_id, user)
        await query.answer(f"تم: {account['address']}", show_alert=True)
        await show_dashboard(update, context, user)
    elif data.startswith("view_inbox_"):
        idx = int(data.split("_")[-1])
        emails = user.get("emails", [])
        if idx >= len(emails):
            return
        email = emails[idx]
        await query.edit_message_text("جاري جلب الرسائل...")
        try:
            token = await refresh_token(email["address"], email["password"])
            if token:
                emails[idx]["token"] = token
                save_user(telegram_id, user)
        except:
            pass
        messages = await get_messages(email["token"])
        if not messages:
            buttons = [[InlineKeyboardButton("رجوع", callback_data="back_dashboard")]]
            await query.edit_message_text(f"{email['address']}\n\nلا توجد رسائل.", reply_markup=InlineKeyboardMarkup(buttons))
            return
        buttons = []
        for msg in messages[:10]:
            subject = msg.get("subject", "بدون موضوع")[:30]
            buttons.append([InlineKeyboardButton(subject, callback_data=f"read_msg_{idx}_{msg['id']}")])
        buttons.append([InlineKeyboardButton("رجوع", callback_data="back_dashboard")])
        await query.edit_message_text(f"{email['address']}\n{len(messages)} رسالة:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("read_msg_"):
        parts = data.split("_")
        idx = int(parts[2])
        msg_id = parts[3]
        emails = user.get("emails", [])
        if idx >= len(emails):
            return
        email = emails[idx]
        msg = await get_message_content(email["token"], msg_id)
        if not msg:
            await query.answer("تعذر القراءة", show_alert=True)
            return
        sender = msg.get("from", {}).get("address", "مجهول")
        subject = msg.get("subject", "بدون موضوع")
        body = re.sub(r'<[^>]+>', '', str(msg.get("text", msg.get("html", "لا يوجد محتوى"))))[:800]
        text = f"رسالة جديدة\n\nالى: {email['address']}\nمن: {sender}\nالموضوع: {subject}\n\n{body}"
        buttons = [[InlineKeyboardButton("رجوع", callback_data=f"view_inbox_{idx}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("delete_email_"):
        idx = int(data.split("_")[-1])
        emails = user.get("emails", [])
        if idx >= len(emails):
            return
        buttons = [[InlineKeyboardButton("نعم احذف", callback_data=f"confirm_delete_{idx}"), InlineKeyboardButton("لا", callback_data="back_dashboard")]]
        await query.edit_message_text(f"حذف: {emails[idx]['address']}?", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("confirm_delete_"):
        idx = int(data.split("_")[-1])
        emails = user.get("emails", [])
        if idx < len(emails):
            try:
                await delete_mailtm_account(emails[idx]["token"], emails[idx]["account_id"])
            except:
                pass
            emails.pop(idx)
            user["emails"] = emails
            save_user(telegram_id, user)
            await query.answer("تم الحذف", show_alert=True)
        await show_dashboard(update, context, user)
    elif data == "logout":
        buttons = [[InlineKeyboardButton("نعم اخرج", callback_data="confirm_logout"), InlineKeyboardButton("لا", callback_data="back_dashboard")]]
        await query.edit_message_text("تسجيل الخروج سيحذف ايميلاتك.", reply_markup=InlineKeyboardMarkup(buttons))
    elif data == "confirm_logout":
        for email in user.get("emails", []):
            try:
                await delete_mailtm_account(email["token"], email["account_id"])
            except:
                pass
        delete_user(telegram_id)
        await query.edit_message_text("تم تسجيل خروجك!\n\nارسل /start للعودة.")
    elif data == "back_dashboard":
        await show_dashboard(update, context, user)

async def check_new_messages(context):
    db = load_db()
    for telegram_id, user in db["users"].items():
        emails = user.get("emails", [])
        for i, email in enumerate(emails):
            try:
                token = await refresh_token(email["address"], email["password"])
                if token:
                    db["users"][telegram_id]["emails"][i]["token"] = token
                messages = await get_messages(email["token"])
                seen = set(email.get("messages_seen", []))
                for msg in messages:
                    if msg["id"] not in seen:
                        sender = msg.get("from", {}).get("address", "مجهول")
                        subject = msg.get("subject", "بدون موضوع")
                        text = f"رسالة جديدة!\n\nالى: {email['address']}\nمن: {sender}\nالموضوع: {subject}"
                        buttons = [[InlineKeyboardButton("قراءة", callback_data=f"read_msg_{i}_{msg['id']}")]]
                        await context.bot.send_message(chat_id=int(telegram_id), text=text, reply_markup=InlineKeyboardMarkup(buttons))
                        seen.add(msg["id"])
                db["users"][telegram_id]["emails"][i]["messages_seen"] = list(seen)
            except Exception as e:
                logger.error(f"Error: {e}")
    save_db(db)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, *args):
        pass

def run_server():
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

def main():
    threading.Thread(target=run_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)]},
        fallbacks=[CommandHandler("start", start)]
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(check_new_messages, interval=15, first=10)
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
