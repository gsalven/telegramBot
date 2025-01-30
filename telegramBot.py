import os
import hmac
import hashlib
import logging
import uuid
import json
import threading
import requests
import asyncio

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv("private.env")

from flask import Flask, request, abort
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
import pymysql

#config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB   = os.getenv("MYSQL_DB")

CALLBACK_URL = "https://platforming.org/nowpayments_webhook"

# payment
PAYMENT_OPTIONS = {
    "option_1": {
        "amount": 20.0,
        "description": "Username Availability Checker ($20)",
        "product_label": "Username Availability Checker (Python)"
    },
    "option_2": {
        "amount": 75.0,
        "description": "Username Autoclaimer and Checker ($75)",
        "product_label": "Username Autoclaimer and Checker (Python)"
    },
    "option_3": {
        "amount": 300.0,
        "description": "Premium Username Autoclaimer and Checker ($300)",
        "product_label": "Premium Username Autoclaimer and Checker (Python)"
    }
}

# products
PRODUCT_FILES = {
    "Username Availability Checker (Python)": "products/checker1/checker1.zip",
    "Username Autoclaimer and Checker (Python)": "products/act1/act1.zip",
    "Premium Username Autoclaimer and Checker (Python)": "products/pact1/pact1.zip"
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# flask
app = Flask(__name__)

application: Application | None = None
bot_instance = None
main_loop = None

# mysql helpers
def get_db_connection():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

def insert_order(user_id, order_id, product_label, chat_id, status="pending"):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO orders (user_id, order_id, product_label, status, chat_id)
            VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (user_id, order_id, product_label, status, chat_id))
        conn.commit()
    except Exception as e:
        logger.error(f"DB insert_order error: {e}")
    finally:
        conn.close()

def get_order_by_order_id(order_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT * FROM orders WHERE order_id = %s LIMIT 1"
            cursor.execute(sql, (order_id,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"DB get_order_by_order_id error: {e}")
        return None
    finally:
        conn.close()

def update_order_status(order_id, new_status):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE orders SET status=%s WHERE order_id=%s"
            cursor.execute(sql, (new_status, order_id))
        conn.commit()
    except Exception as e:
        logger.error(f"DB update_order_status error: {e}")
    finally:
        conn.close()

def get_latest_order_for_user(user_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT * FROM orders
            WHERE user_id=%s
            ORDER BY created_at DESC
            LIMIT 1
            """
            cursor.execute(sql, (user_id,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"DB get_latest_order_for_user error: {e}")
        return None
    finally:
        conn.close()

# another helper func
async def deliver_product_async(chat_id: int, product_label: str):
    logger.info(f"deliver_product_async -> chat_id={chat_id}, product_label={product_label}")

    await bot_instance.send_message(
        chat_id=chat_id,
        text=f"Payment confirmed for '{product_label}'! Sending your file now..."
    )

    file_path = PRODUCT_FILES.get(product_label)
    if file_path:
        try:
            with open(file_path, "rb") as f:
                await bot_instance.send_document(chat_id=chat_id, document=f)
        except Exception as e:
            logger.error(f"Error sending product file: {e}")
    else:
        await bot_instance.send_message(
            chat_id=chat_id,
            text="File not found. Contact support."
        )

# using nowpayments for automated crypto payments. webhooks
@app.route("/nowpayments_webhook", methods=["POST"])
def nowpayments_webhook():
    received_signature = request.headers.get("x-nowpayments-sig", "")
    computed_signature = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        request.data,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(received_signature, computed_signature):
        logger.warning("Invalid NOWPayments signature!")
        abort(400, "Invalid signature")

    data = request.get_json(force=True)
    payment_status = data.get("payment_status", "").lower()
    order_id = data.get("order_id")

    logger.info(f"Webhook received: order_id={order_id}, payment_status={payment_status}")

    success_statuses = ["finished", "confirmed", "paid", "completed"]
    if payment_status in success_statuses:
        row = get_order_by_order_id(order_id)
        if row is None:
            logger.warning(f"No matching order found for order_id={order_id} in DB")
            return "OK", 200

        user_id = row["user_id"]
        chat_id = row["chat_id"]
        product_label = row["product_label"]
        logger.info(f"Payment completed for user_id={user_id}, order_id={order_id}")

        update_order_status(order_id, "paid")

        if main_loop is not None:
            asyncio.run_coroutine_threadsafe(
                deliver_product_async(chat_id, product_label),
                main_loop
            )

    return "OK", 200

# telegram handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to GG's Bot!\n\n"
        "Below is a brief overview of our available products:\n\n"
        "1) Username Availability Checker ($20)\n"
        "   A Python script that constantly checks if a desired username is available. Once available, it stops and notifies you immediately.\n\n"
        "2) Username Autoclaimer and Checker ($75)\n"
        "   A Python script that continuously monitors a desired username and automatically claims it for you as soon as it becomes available.\n\n"
        "3) Premium Username Autoclaimer and Checker ($300)\n"
        "   A Python script that can monitor multiple usernames, automatically claim them when they're available, and uses proxies to avoid spam flags.\n\n"
        "For custom code or scripts, contact our developer @emordnilap.\n"
        "Join our main Telegram channel: https://t.me/turboclaimer\n\n"
        "Select an option below to purchase:",
        reply_markup=ReplyKeyboardRemove()
    )

    keyboard = [
        [InlineKeyboardButton("$20 - Username Availability Checker", callback_data="option_1")],
        [InlineKeyboardButton("$75 - Username Autoclaimer and Checker", callback_data="option_2")],
        [InlineKeyboardButton("$300 - Premium Username Autoclaimer and Checker", callback_data="option_3")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Choose your product:",
        reply_markup=markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    option_key = query.data

    if option_key not in PAYMENT_OPTIONS:
        await query.message.reply_text("Invalid option.")
        return

    amount = PAYMENT_OPTIONS[option_key]["amount"]
    desc = PAYMENT_OPTIONS[option_key]["description"]
    product_label = PAYMENT_OPTIONS[option_key]["product_label"]

    order_id = str(uuid.uuid4())

    invoice_url = create_nowpayments_invoice(amount, order_id, desc)
    if not invoice_url:
        await query.message.reply_text("Error creating invoice. Please try again.")
        return

    insert_order(user_id, order_id, product_label, chat_id, status="pending")

    msg = (
        f"You chose: {desc}\n\n"
        f"To pay ${amount}, use this link:\n{invoice_url}\n\n"
        "When your payment is confirmed, you'll automatically receive your order."
    )
    await query.message.reply_text(msg)

async def check_payment_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    row = get_latest_order_for_user(user_id)
    if not row:
        await update.message.reply_text("No recent orders found.")
        return

    order_id = row["order_id"]
    product_label = row["product_label"]
    status = row["status"]

    if status == "paid":
        await update.message.reply_text(
            f"Your payment for '{product_label}' is already confirmed. "
            "If you didn't receive the file, check your Telegram messages."
        )
    else:
        current_status = get_payment_status_from_nowpayments(order_id)
        if current_status in ["finished", "confirmed", "paid", "completed"]:
            update_order_status(order_id, "paid")
            await update.message.reply_text(
                f"Payment confirmed for '{product_label}'. Sending file now..."
            )
            file_path = PRODUCT_FILES.get(product_label)
            if file_path:
                try:
                    with open(file_path, "rb") as f:
                        await context.bot.send_document(chat_id=update.message.chat_id, document=f)
                except Exception as e:
                    logger.error(f"Error sending file in /check: {e}")
                    await update.message.reply_text("Could not send file. Contact support.")
            else:
                await update.message.reply_text("File not found. Contact support.")
        else:
            await update.message.reply_text(
                f"Payment not confirmed yet. Current status: {current_status}"
            )

def create_nowpayments_invoice(price_amount, order_id, order_description):
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "price_amount": price_amount,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": CALLBACK_URL
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("invoice_url")
    else:
        logger.error(f"NOWPayments invoice creation failed: {resp.text}")
        return None

def get_payment_status_from_nowpayments(order_id):
    """
    A simple function to retrieve the payment status from NOWPayments.
    You can implement this if you'd like an explicit /check call to hit
    the NOWPayments API. For now, it just returns 'unknown' or something
    that your code can handle.
    """
    return "unknown"

# bot && flask start up
def run_flask():
    """Runs Flask on port 8000 in a separate thread."""
    app.run(host="0.0.0.0", port=8000, debug=False)

async def run_telegram_bot():
    """Async function to run PTB polling until stopped."""
    await application.run_polling()

def main():
    global application, bot_instance, main_loop

    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_instance = application.bot

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("check", check_payment_status_command))

    logger.info("Starting Telegram bot with custom event loop + Flask on port=8000.")

    main_loop.create_task(run_telegram_bot())

    main_loop.run_forever()

if __name__ == "__main__":
    main()
