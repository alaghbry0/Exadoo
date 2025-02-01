import logging
import os
import asyncio
from quart import Blueprint, current_app
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from dotenv import load_dotenv

# تحميل متغيرات البيئة
load_dotenv()

# إعداد تسجيل الأخطاء
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# استيراد القيم من .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")

# إنشاء Blueprint للبوت داخل Quart
telegram_bot = Blueprint("telegram_bot", __name__)

# إنشاء كائن `aiogram` Dispatcher
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dispatcher = Dispatcher()


# دالة لمعالجة الأخطاء
async def handle_errors(user_id: int, error_message: str):
    """معالجة الأخطاء أثناء إرسال الرسائل إلى المستخدمين."""
    logging.error(f"❌ خطأ مع المستخدم {user_id}: {error_message}")


# وظيفة /start
@dispatcher.message(Command("start"))
async def start_command(message: Message):
    """إرسال زر فتح التطبيق المصغر عند استخدام /start."""
    user_id = message.from_user.id
    username = message.from_user.username or "غير معروف"

    # إعداد زر التطبيق المصغر
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق المصغر", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])

    # تسجيل بيانات المستخدم
    logging.info(f"✅ /start من المستخدم: {user_id}, Username: {username}")

    # إرسال الرسالة مع الزر
    await message.answer(
        text="مرحبًا بك! اضغط على الزر أدناه لفتح التطبيق المصغر 👇",
        reply_markup=keyboard
    )


# دالة إرسال رسالة إلى المستخدم عبر البوت
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


# تشغيل `aiogram` داخل `Quart`
async def init_bot():
    """ربط بوت `aiogram` مع `Quart` عند تشغيل التطبيق."""
    logging.info("✅ Telegram Bot Ready!")


# تشغيل `aiogram` في سيرفر `Quart`
async def start_telegram_bot():
    """تشغيل `aiogram` Dispatcher في الخلفية."""
    loop = asyncio.get_event_loop()
    loop.create_task(dispatcher.start_polling(bot))
