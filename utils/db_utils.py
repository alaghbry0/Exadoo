import logging
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from database.db_queries import add_user, get_user, add_scheduled_task, update_subscription
from config import TELEGRAM_BOT_TOKEN
import asyncio

# تهيئة بوت تيليجرام
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)


# ----------------- 🔹 إضافة المستخدم إلى القناة ----------------- #

async def add_user_to_channel(telegram_id: int, subscription_type_id: int, db_pool):
    """
    إضافة المستخدم إلى القناة أو إرسال رابط دعوة في حال لم يكن موجودًا.
    """
    try:
        async with db_pool.acquire() as connection:
            # جلب تفاصيل الاشتراك
            subscription_type = await connection.fetchrow(
                "SELECT channel_id, name FROM subscription_types WHERE id = $1", subscription_type_id
            )

        if not subscription_type:
            logging.error(f"❌ نوع الاشتراك {subscription_type_id} غير موجود.")
            return False

        channel_id = int(subscription_type['channel_id'])  # تأكد من تحويل channel_id إلى int
        channel_name = subscription_type['name']

        # التحقق مما إذا كان المستخدم موجودًا بالفعل في القناة
        try:
            member = await telegram_bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
            if member.status in ['member', 'administrator', 'creator']:
                logging.info(f"✅ المستخدم {telegram_id} موجود بالفعل في القناة {channel_id}.")
                await send_message(telegram_id, f"تم تجديد اشتراكك في قناة {channel_name} بنجاح! 🎉")
                return True
        except TelegramAPIError:
            logging.warning(f"⚠️ المستخدم {telegram_id} غير موجود في القناة {channel_id}.")

        # إزالة الحظر إن وجد
        try:
            await telegram_bot.unban_chat_member(chat_id=channel_id, user_id=telegram_id)
        except TelegramAPIError:
            logging.warning(f"⚠️ لم يتمكن من إزالة الحظر عن المستخدم {telegram_id}.")

        # إنشاء رابط دعوة
        invite_link = await telegram_bot.create_chat_invite_link(
            chat_id=channel_id, member_limit=1
        )

        # إرسال رابط الدعوة
        success = await send_message(
            telegram_id,
            f"✅ تم تفعيل اشتراكك بنجاح! يمكنك الانضمام إلى قناة {channel_name} عبر هذا الرابط:\n{invite_link.invite_link}"
        )
        return success

    except TelegramAPIError as e:
        logging.error(f"❌ خطأ أثناء إرسال رابط الدعوة للمستخدم {telegram_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء إضافة المستخدم {telegram_id}: {e}")
        return False

# ----------------- 🔹 إزالة المستخدم من القناة ----------------- #

async def remove_user_from_channel(connection, telegram_id: int, channel_id: int):
    """
    إزالة المستخدم من القناة وإرسال إشعار له.
    """
    try:
        # جلب اسم القناة
        subscription_type = await connection.fetchrow(
            "SELECT name FROM subscription_types WHERE channel_id = $1", channel_id
        )

        if not subscription_type:
            logging.error(f"❌ لم يتم العثور على القناة {channel_id}.")
            return False

        channel_name = subscription_type['name']

        # محاولة إزالة المستخدم من القناة
        try:
            await telegram_bot.ban_chat_member(chat_id=channel_id, user_id=telegram_id)
            logging.info(f"✅ تمت إزالة المستخدم {telegram_id} من القناة {channel_id}.")
        except TelegramAPIError as e:
            logging.error(f"❌ فشل إزالة المستخدم {telegram_id} من القناة {channel_id}: {e}")
            return False

        # إرسال إشعار للمستخدم
        success = await send_message(
            telegram_id,
            f"⚠️ تم إخراجك من قناة '{channel_name}' بسبب انتهاء الاشتراك.\n"
            "🔄 يمكنك التجديد للعودة مجددًا!"
        )
        return success

    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء إزالة المستخدم {telegram_id} من القناة {channel_id}: {e}")
        return False


# ----------------- 🔹 إرسال رسالة للمستخدم ----------------- #

async def send_message(telegram_id: int, message: str):
    """
    إرسال رسالة مباشرة للمستخدم عبر البوت.
    """
    try:
        # التحقق مما إذا كانت دردشة المستخدم نشطة
        if not await is_chat_active(telegram_id):
            logging.warning(f"⚠️ المستخدم {telegram_id} ليس لديه محادثة نشطة مع البوت.")
            return False

        await telegram_bot.send_message(chat_id=telegram_id, text=message)
        logging.info(f"📩 تم إرسال الرسالة إلى المستخدم {telegram_id}.")
        return True

    except TelegramAPIError as e:
        if "chat not found" in str(e).lower():
            logging.error(f"⚠️ المستخدم {telegram_id} لم يبدأ المحادثة أو قام بحظر البوت.")
        else:
            logging.error(f"❌ خطأ في Telegram API أثناء إرسال الرسالة إلى {telegram_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء إرسال الرسالة إلى {telegram_id}: {e}")
        return False


# ----------------- 🔹 التحقق من حالة المحادثة ----------------- #

async def is_chat_active(telegram_id: int):
    """
    التحقق مما إذا كانت دردشة المستخدم مع البوت نشطة.
    """
    try:
        chat = await telegram_bot.get_chat(chat_id=telegram_id)
        return chat is not None
    except TelegramAPIError as e:
        if "chat not found" in str(e).lower():
            logging.warning(f"⚠️ المستخدم {telegram_id} لم يبدأ المحادثة مع البوت.")
        else:
            logging.error(f"❌ خطأ في Telegram API أثناء التحقق من حالة محادثة المستخدم {telegram_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء التحقق من حالة محادثة المستخدم {telegram_id}: {e}")
        return False


# ----------------- 🔹 إغلاق جلسة بوت تيليجرام ----------------- #

async def close_telegram_bot_session():
    """
    إغلاق جلسة Telegram Bot API.
    """
    try:
        await telegram_bot.session.close()
        logging.info("✅ تم إغلاق جلسة Telegram Bot API بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق جلسة Telegram Bot API: {e}")
