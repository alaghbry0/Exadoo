import logging
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from database.db_queries import add_user, get_user, add_scheduled_task, update_subscription
from config import TELEGRAM_BOT_TOKEN
import asyncio
import time

# تهيئة بوت تيليجرام
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)


# ----------------- 🔹 إضافة المستخدم إلى القناة ----------------- #

async def generate_channel_invite_link(telegram_id: int, channel_id: int, channel_name: str):  # اسم أكثر عمومية
    """
    توليد رابط دعوة لمستخدم لقناة محددة.
    """
    try:
        # إزالة الحظر إن وجد
        try:
            await telegram_bot.unban_chat_member(chat_id=channel_id, user_id=telegram_id)
            logging.info(f"Attempted to unban user {telegram_id} from channel {channel_id}.")
        except TelegramAPIError as e:
            logging.warning(f"⚠️ Could not unban user {telegram_id} from channel {channel_id}: {e.message}")

        expire_date = int(time.time()) + (30 * 24 * 60 * 60)  # شهر واحد
        invite_link_obj = await telegram_bot.create_chat_invite_link(
            chat_id=channel_id,
            creates_join_request=True,
            name=f"اشتراك {telegram_id} في {channel_name}",
            expire_date=expire_date
        )
        invite_link_str = invite_link_obj.invite_link
        logging.info(
            f"✅ تم إنشاء رابط دعوة للمستخدم {telegram_id} لقناة {channel_name} ({channel_id}): {invite_link_str}")

        return {
            "success": True,
            "invite_link": invite_link_str if invite_link_str else "",
            "message": f"تم إنشاء رابط دعوة لك للانضمام إلى قناة {channel_name}."
        }
    except TelegramAPIError as e:
        logging.error(f"❌ خطأ API أثناء إنشاء رابط دعوة للمستخدم {telegram_id} لقناة {channel_id}: {e}")
        return {"success": False, "invite_link": None, "error": str(e)}
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء معالجة القناة {channel_id} للمستخدم {telegram_id}: {e}")
        return {"success": False, "invite_link": None, "error": str(e)}


async def send_message_to_user(telegram_id: int, message_text: str):
    """
    إرسال رسالة نصية إلى مستخدم تليجرام.
    """
    try:
        await telegram_bot.send_message(chat_id=telegram_id, text=message_text, parse_mode="HTML")  # أضفت parse_mode
        logging.info(f"✅ Message sent to {telegram_id}")
        return True
    except TelegramAPIError as e:
        logging.error(f"❌ Failed to send message to {telegram_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ Unexpected error sending message to {telegram_id}: {e}")
        return False


async def remove_user_from_channel(connection, telegram_id: int, channel_id: int):
    """
    إزالة المستخدم من القناة وإرسال إشعار له.
    """
    try:
        # جلب اسم القناة
        # بما أن scheduled_tasks.channel_id لم يعد مقيدًا بـ subscription_types
        # نحتاج لجلب اسم القناة من subscription_type_channels أو جدول Channels عام إذا كان لديك
        channel_info = await connection.fetchrow(
            """SELECT stc.channel_name, st.name as subscription_type_name
               FROM subscription_type_channels stc
               JOIN subscription_types st ON stc.subscription_type_id = st.id
               WHERE stc.channel_id = $1 LIMIT 1""",
            channel_id
        )

        # إذا لم يتم العثور عليه في subscription_type_channels، قد يكون قناة قديمة أو خطأ ما.
        # يمكنك وضع اسم افتراضي أو تسجيل خطأ.
        channel_display_name = channel_info['channel_name'] if channel_info and channel_info[
            'channel_name'] else f"القناة {channel_id}"
        subscription_type_name_for_message = channel_info['subscription_type_name'] if channel_info else "الاشتراك"

        # محاولة إزالة المستخدم من القناة
        try:
            await telegram_bot.ban_chat_member(chat_id=channel_id, user_id=telegram_id)
            logging.info(f"✅ تمت إزالة المستخدم {telegram_id} من القناة {channel_display_name} ({channel_id}).")
        except TelegramAPIError as e:
            logging.error(f"❌ فشل إزالة المستخدم {telegram_id} من القناة {channel_display_name} ({channel_id}): {e}")
            # لا ترجع False هنا مباشرة، فقد نرغب في إرسال الرسالة على أي حال
            pass

        # إرسال إشعار للمستخدم
        message_to_user = (
            f"⚠️ تم إخراجك من قناة '{channel_display_name}' (التابعة لاشتراك '{subscription_type_name_for_message}') بسبب انتهاء الاشتراك.\n"
            "🔄 يمكنك التجديد للعودة مجددًا!"
        )
        await send_message_to_user(telegram_id, message_to_user)
        return True

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
