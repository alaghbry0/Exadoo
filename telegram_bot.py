import logging
import os
import asyncio
import json
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from dotenv import load_dotenv
from quart import Blueprint  # ✅ استيراد `Blueprint` لاستخدامه في `app.py`

# 🔹 تحميل متغيرات البيئة
load_dotenv()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 🔹 استيراد القيم من .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")

# ✅ التحقق من القيم المطلوبة في البيئة
if not TELEGRAM_BOT_TOKEN or not WEB_APP_URL:
    raise ValueError("❌ خطأ: يجب ضبط جميع المتغيرات البيئية!")

# 🔹 إعداد Aiogram 3.x
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# 🔹 إنشاء Blueprint لاستخدامه في `app.py`
telegram_bot_bp = Blueprint("telegram_bot", __name__)  # ✅ تغيير الاسم إلى `telegram_bot_bp`

# 🔹 إزالة Webhook تمامًا قبل تشغيل Polling
async def remove_webhook():
    """🔄 إزالة Webhook حتى يعمل Polling"""
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ تم إزالة Webhook بنجاح!")

# 🔹 وظيفة /start
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

# 🔹 تشغيل Polling بدلاً من Webhook
async def start_bot():
    """✅ تشغيل Polling بدلاً من Webhook"""
    await remove_webhook()
    logging.info("🚀 بدء تشغيل Polling للبوت...")
    await dp.start_polling(bot)

# 🔹 تشغيل البوت فقط عند تشغيل الملف مباشرةً
if __name__ == "__main__":
    asyncio.run(start_bot())
