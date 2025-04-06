import logging
import os
import asyncio
import sys
import json
import aiohttp  # ✅ استيراد `aiohttp` لإرسال الطلبات
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
CHANNEL_URL = os.getenv("CHANNEL_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL")  # ✅ تحميل رابط `/api/subscribe`
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ✅ تحميل `WEBHOOK_SECRET`

# ✅ التحقق من القيم المطلوبة في البيئة
if not TELEGRAM_BOT_TOKEN or not WEB_APP_URL or not SUBSCRIBE_URL or not WEBHOOK_SECRET:
    raise ValueError("❌ خطأ: تأكد من ضبط جميع المتغيرات البيئية!")

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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram import Bot, Dispatcher, types
import logging

@dp.message(Command("start"))
async def start_command(message: types.Message):
    """✅ إرسال زر فتح التطبيق المصغر عند استخدام /start (مبسط)"""
    user_id = message.from_user.id
    full_name = message.from_user.full_name or "مستخدم عزيز"

    # ✅ لوحة مفاتيح مبسطة بزر واحد فقط (فتح التطبيق المصغر)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق", web_app=WebAppInfo(url=WEB_APP_URL))],
        # ✅ تم حذف زر "فتح القناة" مؤقتًا للتبسيط
    ])

    logging.info(f"✅ /start من المستخدم: {user_id}, Full Name: {full_name}")

    welcome_text = (
        f"👋 مرحبًا {full_name}!\n\n"
        "مرحبًا بك في **@Exaado** \n"
        "هنا يمكنك إدارة اشتراكاتك في قنواتنا بسهولة.\n\n"
        "نتمنى لك تجربة رائعة! 🚀"
    )

    await message.answer(text=welcome_text, reply_markup=keyboard, parse_mode="Markdown")


# 🔹 وظيفة معدلة لمعالجة الدفع الناجح
async def send_payment_to_subscribe_api(
        telegram_id: int,
        plan_id: int,
        payment_id: str,
        payment_token: str,
        full_name: str,  # إضافة الاسم الكامل
        username: str,    # إضافة اسم المستخدم
        retries=3
):
    """✅ إرسال بيانات الدفع مع المعلومات الجديدة"""
    headers = {
        "Authorization": f"Bearer {WEBHOOK_SECRET}",
        "Content-Type": "application/json"
    }

    payload = {
        "telegram_id": telegram_id,
        "subscription_plan_id": plan_id,
        "payment_id": payment_id,
        "payment_token": payment_token,
        "full_name": full_name,
        "telegram_username": username
    }

    async with aiohttp.ClientSession() as session:
        for attempt in range(1, retries + 1):
            try:
                logging.info(f"🚀 إرسال بيانات الاشتراك (المحاولة {attempt}/{retries})...")

                async with session.post(
                    SUBSCRIBE_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:

                    if response.status == 200:
                        logging.info(f"✅ تم تحديث الاشتراك لـ {telegram_id}")
                        return True

                    response_text = await response.text()
                    logging.error(f"❌ فشل الاستجابة ({response.status}): {response_text}")

            except Exception as e:
                logging.error(f"❌ خطأ في المحاولة {attempt}/{retries}: {str(e)}")

            if attempt < retries:
                await asyncio.sleep(2 ** attempt)

        logging.critical("🚨 فشل جميع المحاولات!")
        return False

@dp.message()
async def handle_successful_payment(message: types.Message):
    """✅ معالجة الدفع الناجح باستخدام البيانات المضمنة"""
    payment = message.successful_payment
    if not payment:
        return

    try:
        logging.info(f"📥 استلام دفعة ناجحة من {message.from_user.id}")

        # استخراج البيانات مباشرة من payload الفاتورة
        payload = json.loads(payment.invoice_payload)
        telegram_id = payload.get("userId")
        plan_id = payload.get("planId")
        payment_id = payment.telegram_payment_charge_id
        payment_token = payload.get("paymentToken")
        full_name = payload.get("fullName")
        username = payload.get("telegramUsername")

        # التحقق من البيانات الأساسية
        required_fields = [
            (telegram_id, "telegram_id"),
            (plan_id, "plan_id"),
            (payment_id, "payment_id"),
            (payment_token, "payment_token")
        ]

        missing_fields = [name for value, name in required_fields if not value]
        if missing_fields:
            logging.error(f"❌ بيانات ناقصة: {', '.join(missing_fields)}")
            return

        # إرسال البيانات مباشرة دون التحقق من قاعدة البيانات
        success = await send_payment_to_subscribe_api(
            telegram_id=telegram_id,
            plan_id=plan_id,
            payment_id=payment_id,
            payment_token=payment_token,
            full_name=full_name or "غير معروف",
            username=username or "غير معروف"
        )

        if not success:
            logging.error("❌ فشل إرسال البيانات إلى خدمة الاشتراك")

    except json.JSONDecodeError as e:
        logging.error(f"❌ خطأ في تنسيق JSON: {str(e)}")
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع: {str(e)}")

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
is_bot_running = False

async def start_bot():
    global is_bot_running
    if is_bot_running:
        logging.warning("⚠️ البوت يعمل بالفعل! تجاهل تشغيل Polling مرة أخرى.")
        return

    is_bot_running = True
    await remove_webhook()
    logging.info("🚀 بدء تشغيل Polling للبوت...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ خطأ أثناء تشغيل Polling: {e}")
        sys.exit(1)  # إغلاق التطبيق في حالة فشل التشغيل