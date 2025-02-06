import logging
import os
import asyncio
import json
from quart import Blueprint
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from dotenv import load_dotenv

# 🔹 تحميل متغيرات البيئة
load_dotenv()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 🔹 استيراد القيم من .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# ✅ التحقق من القيم المطلوبة في البيئة
if not TELEGRAM_BOT_TOKEN or not WEBHOOK_SECRET or not WEB_APP_URL or not WEBHOOK_URL:
    raise ValueError("❌ خطأ: يجب ضبط جميع المتغيرات البيئية!")

# 🔹 إنشاء Blueprint للبوت داخل Quart
telegram_bot = Blueprint("telegram_bot", __name__)

# 🔹 إعداد Aiogram 3.x
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# 🔹 ربط Webhook مع `Dispatcher`
async def start_bot():
    """✅ بدء تشغيل Webhook مع Aiogram"""
    logging.info("🚀 بدء تشغيل Webhook للبوت...")

    # ✅ حذف Webhook القديم وتحديثه
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)

    # ✅ تشغيل Webhook Handlers مع Quart
    webhook_request_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    telegram_bot.add_url_rule("/webhook", "webhook", webhook_request_handler.handle, methods=["POST"])

    logging.info("✅ Webhook للبوت جاهز!")

# 🔹 دالة /start
@dp.message(Command("start"))
async def start_command(message: Message):
    """✅ إرسال زر فتح التطبيق المصغر عند استخدام /start"""
    user_id = message.from_user.id
    username = message.from_user.username or "غير معروف"

    # ✅ إعداد زر التطبيق المصغر
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق المصغر", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])

    # ✅ تسجيل بيانات المستخدم
    logging.info(f"✅ /start من المستخدم: {user_id}, Username: {username}")

    # ✅ إرسال الرسالة مع الزر
    await message.answer(text="مرحبًا بك! اضغط على الزر أدناه لفتح التطبيق المصغر 👇", reply_markup=keyboard)

# 🔹 دالة إرسال رسالة إلى المستخدم عبر البوت
async def send_message_to_user(user_id: int, message_text: str):
    """✅ إرسال رسالة مباشرة إلى مستخدم عبر دردشة البوت"""
    if not message_text:
        logging.warning(f"⚠️ لم يتم إرسال الرسالة إلى المستخدم {user_id} لأن المحتوى فارغ.")
        return

    try:
        await bot.send_message(chat_id=user_id, text=message_text)
        logging.info(f"📩 تم إرسال الرسالة إلى المستخدم {user_id}: {message_text}")
    except TelegramAPIError as e:
        await handle_errors(user_id, f"Telegram API Error: {e}")
    except Exception as e:
        await handle_errors(user_id, f"Unexpected error: {e}")

# 🔹 دالة معالجة الأخطاء
async def handle_errors(user_id: int, error_message: str):
    """✅ معالجة الأخطاء أثناء إرسال الرسائل إلى المستخدمين"""
    logging.error(f"❌ خطأ مع المستخدم {user_id}: {error_message}")

# 🔹 وظيفة pre_checkout_query للتحقق من الدفع
@dp.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: types.PreCheckoutQuery):
    """✅ التحقق من صحة الفاتورة قبل إتمام الدفع"""
    try:
        logging.info(f"📥 استلام pre_checkout_query من {pre_checkout.from_user.id}: {pre_checkout}")

        # ✅ التحقق من صحة invoice_payload
        payload = json.loads(pre_checkout.invoice_payload)
        if not payload.get("userId") or not payload.get("planId"):
            logging.error("❌ `invoice_payload` غير صالح!")
            await bot.answer_pre_checkout_query(pre_checkout.id, ok=False, error_message="بيانات الدفع غير صالحة!")
            return

        # ✅ إذا كان كل شيء صحيح، الموافقة على الدفع
        await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)
        logging.info(f"✅ تمت الموافقة على الدفع لـ {pre_checkout.from_user.id}")

    except Exception as e:
        logging.error(f"❌ خطأ في pre_checkout_query: {e}")
        await bot.answer_pre_checkout_query(pre_checkout.id, ok=False, error_message="حدث خطأ غير متوقع")

# 🔹 إغلاق جلسة بوت تيليجرام عند إيقاف التطبيق
async def close_bot_session():
    """✅ إغلاق جلسة بوت تيليجرام"""
    try:
        await bot.session.close()
        logging.info("✅ تم إغلاق جلسة بوت تيليجرام بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق جلسة بوت تيليجرام: {e}")
