import logging
import os
import asyncio
from quart import Blueprint
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from dotenv import load_dotenv

# 🔹 تحميل متغيرات البيئة
load_dotenv()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 🔹 استيراد القيم من .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# ✅ التحقق من القيم المطلوبة في البيئة
if not TELEGRAM_BOT_TOKEN or not WEBHOOK_SECRET or not WEB_APP_URL:
    raise ValueError("❌ خطأ: يجب ضبط TELEGRAM_BOT_TOKEN و WEBHOOK_SECRET و WEB_APP_URL في البيئة!")

# 🔹 إنشاء Blueprint للبوت داخل Quart
telegram_bot = Blueprint("telegram_bot", __name__)

# 🔹 إعداد Aiogram 3.x
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()



# 🔹 دالة معالجة الأخطاء أثناء إرسال الرسائل
async def handle_errors(user_id: int, error_message: str):
    """معالجة الأخطاء أثناء إرسال الرسائل إلى المستخدمين."""
    logging.error(f"❌ خطأ مع المستخدم {user_id}: {error_message}")


# 🔹 وظيفة /start
@dp.message(Command("start"))
async def start_command(message: Message):
    """إرسال زر فتح التطبيق المصغر عند استخدام /start."""
    user_id = message.from_user.id
    username = message.from_user.username or "غير معروف"

    # ✅ إعداد زر التطبيق المصغر
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق المصغر", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])

    # ✅ تسجيل بيانات المستخدم
    logging.info(f"✅ /start من المستخدم: {user_id}, Username: {username}")

    # ✅ إرسال الرسالة مع الزر
    await message.answer(
        text="مرحبًا بك! اضغط على الزر أدناه لفتح التطبيق المصغر 👇",
        reply_markup=keyboard
    )


# 🔹 دالة إرسال رسالة إلى المستخدم عبر البوت
async def send_message_to_user(user_id: int, message_text: str):
    """إرسال رسالة مباشرة إلى مستخدم عبر دردشة البوت."""
    if not message_text:
        logging.warning(f"⚠️ لم يتم إرسال الرسالة إلى المستخدم {user_id} لأن المحتوى فارغ.")
        return

    try:
        await bot.send_message(chat_id=user_id, text=message_text)
        logging.info(f"📩 تم إرسال الرسالة إلى المستخدم {user_id}: {message_text}")

    except TelegramAPIError as e:
        if "chat not found" in str(e).lower():
            await handle_errors(user_id, "المستخدم لم يبدأ المحادثة مع البوت أو قام بحظره.")
        else:
            await handle_errors(user_id, f"Telegram API Error: {e}")
    except Exception as e:
        await handle_errors(user_id, f"Unexpected error: {e}")


# 🔹 إعداد Webhook مع `retry`
async def setup_webhook():
    """✅ ضبط Webhook فقط عند الحاجة"""
    webhook_url = os.getenv("WEBHOOK_URL")

    if not webhook_url or not WEBHOOK_SECRET:
        logging.error("❌ `WEBHOOK_URL` أو `WEBHOOK_SECRET` غير مضبوط! تحقق من الإعدادات.")
        return False

    try:
        # ✅ تحقق مما إذا كان Webhook مضبوطًا بالفعل
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url == webhook_url:
            logging.info("✅ Webhook مضبوط مسبقًا، لا حاجة لتحديثه.")
            return True  # لا حاجة لإعادة التعيين

        # ✅ إذا لم يكن مضبوطًا، قم بتحديثه
        logging.info("🔄 تحديث Webhook لأن العنوان مختلف...")
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "pre_checkout_query", "successful_payment"]
        )
        logging.info(f"✅ تم تعيين Webhook بنجاح على {webhook_url} مع `successful_payment` و `pre_checkout_query`")
        return True

    except Exception as e:
        logging.error(f"❌ فشل تعيين Webhook: {e}")
        return False

@dp.message(Command("setwebhook"))
async def cmd_setwebhook(message: types.Message):
    success = await setup_webhook()
    if success:
        await message.answer("✅ Webhook تم ضبطه بنجاح!")
    else:
        await message.answer("❌ فشل في تعيين Webhook. يرجى التحقق من السجلات.")


# 🔹 تشغيل aiogram داخل Quart
async def init_bot():
    """✅ تشغيل البوت بدون إعادة ضبط Webhook إذا لم يكن ضروريًا"""
    try:
        webhook_info = await bot.get_webhook_info()

        if webhook_info.url != os.getenv("WEBHOOK_URL"):  # ✅ تحديث Webhook فقط إذا لم يكن مضبوطًا
            logging.info("🔄 تحديث Webhook لأن العنوان مختلف...")
            await setup_webhook()
        else:
            logging.info("✅ Webhook مضبوط مسبقًا، لا حاجة للتحديث.")

        logging.info("✅ Telegram Bot Ready!")

    except Exception as e:
        logging.error(f"❌ Failed to initialize Telegram Bot: {e}")


@dp.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: types.PreCheckoutQuery):
    """✅ التحقق من صحة الفاتورة قبل إتمام الدفع"""
    try:
        # التحقق من invoice_payload (مثال)
        payload = json.loads(pre_checkout.invoice_payload)
        if not payload.get("userId"):
            await bot.answer_pre_checkout_query(
                pre_checkout.id,
                ok=False,
                error_message="بيانات الدفع غير صالحة"
            )
            return

        # ✅ إذا كان كل شيء صحيح، الموافقة على الدفع
        await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)
        logging.info(f"✅ تمت الموافقة على الدفع لـ {pre_checkout.from_user.id}")

    except Exception as e:
        logging.error(f"❌ خطأ في pre_checkout_query: {e}")
        await bot.answer_pre_checkout_query(
            pre_checkout.id,
            ok=False,
            error_message="حدث خطأ غير متوقع"
        )

# ✅ إغلاق جلسة بوت تيليجرام عند إيقاف التطبيق
async def close_bot_session():
    try:
        await bot.session.close()
        logging.info("✅ تم إغلاق جلسة بوت تيليجرام بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق جلسة بوت تيليجرام: {e}")


# 🔹 تشغيل aiogram في سيرفر Quart
async def start_bot():
    """✅ بدء تشغيل Webhook فقط إذا لم يكن مضبوطًا مسبقًا"""
    logging.info("🚀 التحقق من Webhook قبل التعيين...")
    await setup_webhook()  # ✅ التحقق من Webhook قبل تعيينه
