import logging
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNotFound,
    TelegramForbiddenError,
)
from database.db_queries import add_user, get_user, add_scheduled_task, update_subscription
from config import TELEGRAM_BOT_TOKEN
import asyncio
import time
from aiogram.enums import ChatMemberStatus
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

            await telegram_bot.unban_chat_member(
                chat_id=channel_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
            logging.info(f"User {telegram_id} unbanned from channel {channel_id} (if was banned).")

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


async def remove_users_from_channel(telegram_id: int, channel_id: int) -> bool:
    """
    Removes a user from a channel and sends them a notification.
    Uses the globally defined bot instance.
    """
    message_text_template = ( # استخدام قالب لتسهيل تعديل اسم القناة
        "🔔 تنبيه مهم\n\n"
        "تم الغاء اشتراكك وازالتك من {channel_display_name}\n"
        "لتتمكن من الانضمام مجددًا، يرجى تجديد اشتراكك."
    )
    channel_display_name = f"`{channel_id}`" # اسم افتراضي

    try:
        # حاول الحصول على اسم القناة لعرضه في الرسالة
        try:
            channel_info = await telegram_bot.get_chat(channel_id) # استخدام الكائن العام
            title = getattr(channel_info, "title", None)
            if title:
                channel_display_name = f'"{title}"'
        except TelegramNotFound: # خطأ محدد لعدم العثور على القناة
            logging.warning(f"Channel {channel_id} not found when fetching title for notification.")
        except Exception as e_title: # أي خطأ آخر أثناء جلب العنوان
            logging.warning(f"Could not get channel info for {channel_id} to get title: {e_title}")

        # تكوين الرسالة النهائية
        final_message_text = message_text_template.format(channel_display_name=channel_display_name)

        logging.info(f"Attempting to ban user {telegram_id} from channel {channel_id}")
        await telegram_bot.ban_chat_member( # استخدام الكائن العام
            chat_id=channel_id,
            user_id=telegram_id,
            revoke_messages=False,
        )
        logging.info(f"User {telegram_id} banned from channel {channel_id}.")

        logging.info(f"Attempting to unban user {telegram_id} to allow rejoining")
        await telegram_bot.unban_chat_member( # استخدام الكائن العام
            chat_id=channel_id,
            user_id=telegram_id,
            only_if_banned=True,
        )
        logging.info(f"User {telegram_id} unbanned (if was banned).")

        logging.info(f"Sending notification to user {telegram_id}")
        await telegram_bot.send_message(chat_id=telegram_id, text=final_message_text) # استخدام الكائن العام
        logging.info(f"Notification sent to user {telegram_id}.")
        return True

    except TelegramNotFound as e: # إذا لم يتم العثور على المستخدم أثناء الحظر أو القناة
        logging.warning(
            f"Resource (user {telegram_id} or channel {channel_id}) not found during ban/kick operation: {e}. "
            "Assuming user effectively removed. Attempting to send notification if user context is available."
        )
        try:
            await telegram_bot.send_message(chat_id=telegram_id, text=final_message_text)
            logging.info(f"Notification sent to user {telegram_id} despite earlier resource not found issue.")
        except TelegramForbiddenError: # مثل BotBlocked
             logging.warning(f"Bot blocked by user {telegram_id}, cannot send notification after resource not found error.")
        except TelegramNotFound: # إذا كان المستخدم غير موجود حقًا للرسالة
             logging.warning(f"User {telegram_id} not found when attempting to send notification after initial resource not found error.")
        except Exception as notify_err:
             logging.error(f"Failed to send notification to {telegram_id} after resource not found error: {notify_err}")
        return True # يعتبر ناجحًا لأن المستخدم لم يكن موجودًا لطرده

    except TelegramForbiddenError as e:
        # ممنوع (bot blocked by user أو صلاحيات ناقصة في القناة)
        msg_lower = str(e).lower()
        if "bot was blocked by the user" in msg_lower: # رسالة خطأ أكثر تحديدًا لـ Aiogram 3
            logging.warning(f"Bot was blocked by user {telegram_id}. Kick may have succeeded.")
        elif "chat_write_forbidden" in msg_lower: # البوت لا يستطيع الكتابة في الشات (للمستخدم)
             logging.warning(f"Bot is forbidden from writing to user {telegram_id}.")
        elif "need administrator rights" in msg_lower or "not enough rights" in msg_lower:
            logging.error(f"Bot lacks administrator rights in channel {channel_id} to perform action: {e}")
            return False # فشل حقيقي بسبب الصلاحيات
        else:
            logging.error(f"TelegramForbiddenError in channel {channel_id} for user {telegram_id}: {e}")
        # في معظم حالات Forbidden (مثل حظر البوت من قبل المستخدم)، يمكن اعتبار عملية الإزالة ناجحة (أو غير ضرورية)
        return True # ما لم يكن خطأ صلاحيات في القناة

    except TelegramAPIError as e:
        logging.error(
            f"Telegram API error for user {telegram_id}, channel {channel_id}: {e}",
            exc_info=True
        )
        return False

    except Exception as e:
        logging.error(
            f"Unexpected error for user {telegram_id}, channel {channel_id}: {e}",
            exc_info=True
        )
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
