# =============== utils/db_utils.py (النسخة المعدلة) ===============
import logging
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
    TelegramForbiddenError,  # مثل BotBlockedByUser, UserDeactivated, ChatNotFound (إذا كان معرّف الدردشة خاصًا)
    TelegramBadRequest,
    TelegramNotFound,
)

import asyncio
import time
import re
from aiogram.enums import ChatMemberStatus


async def send_message_to_user(bot: Bot, telegram_id: int, message_text: str, parse_mode: str = "HTML"): # إضافة parse_mode كمعامل
    """
    إرسال رسالة نصية إلى مستخدم تليجرام مع دعم تنسيق HTML.
    يثير استثناءات Telegram API مباشرة ليتم التعامل معها من قبل المتصل.
    """
    try:
        await bot.send_message(chat_id=telegram_id, text=message_text, parse_mode=parse_mode)
        logging.info(f"📩 Message sent successfully to user {telegram_id}.")
        # لا نعيد قيمة هنا، فالمتصل سيفترض النجاح إذا لم يتم إثارة استثناء
    except TelegramRetryAfter as e: # سابقًا FloodWaitError
        logging.warning(f"Flood control for user {telegram_id}: Retry after {e.retry_after}s. Error: {e}")
        raise # أعد إثارة الاستثناء ليتم التعامل معه في _process_batch
    except TelegramForbiddenError as e: # BotKicked, UserDeactivated, CantTalkWithBots, etc.
        logging.warning(f"Forbidden to send to user {telegram_id}: {e}")
        raise
    except TelegramBadRequest as e: # ChatNotFound (for public), PeerIdInvalid, UserIsBot, etc.
        logging.warning(f"Bad request sending to user {telegram_id}: {e}")
        raise
    except TelegramAPIError as e: # Catch-all for other Telegram API errors
        logging.error(f"Telegram API error sending to user {telegram_id}: {e}", exc_info=True)
        raise
    except Exception as e: # أخطاء غير متوقعة غير متعلقة بـ Telegram API
        logging.error(f"Unexpected non-API error sending message to {telegram_id}: {e}", exc_info=True)
        # يمكنك اختيار إثارة هذا كنوع خطأ مخصص أو خطأ عام
        raise RuntimeError(f"Unexpected error sending message: {e}") from e

# ✅ تعديل: إضافة `bot: Bot` كأول معامل
async def generate_shared_invite_link_for_channel(
        bot: Bot,
        channel_id: int,
        channel_name: str,
        link_name_prefix: str = "الاشتراك في"
):
    """
    توليد رابط دعوة مشترك لقناة محددة، مع معالجة خطأ Flood Wait.
    """
    max_retries = 3
    current_retry = 0
    base_wait_time = 5

    while current_retry < max_retries:
        try:
            # ✅ تعديل: استخدام `bot` المستلم
            invite_link_obj = await bot.create_chat_invite_link(
                chat_id=channel_id,
                creates_join_request=True,
                name=f"الرابط الدائم لـ {channel_name}"
            )
            invite_link_str = invite_link_obj.invite_link

            logging.info(f"🔗 تم إنشاء رابط دعوة مشترك لقناة '{channel_name}' ({channel_id}): {invite_link_str}")
            return {
                "success": True,
                "invite_link": invite_link_str if invite_link_str else "",
                "message": f"تم إنشاء رابط دعوة مشترك للانضمام إلى قناة {channel_name}."
            }
        except TelegramAPIError as e:
            error_message = str(e).lower()
            if "too many requests" in error_message or "flood control" in error_message:
                wait_seconds_match = re.search(r"retry after (\d+)", error_message)
                wait_seconds = int(wait_seconds_match.group(1)) if wait_seconds_match else base_wait_time * (
                            2 ** current_retry)
                logging.warning(
                    f"⚠️ Flood control exceeded... Retrying in {wait_seconds} seconds..."
                )
                await asyncio.sleep(wait_seconds + 1)
                current_retry += 1
            else:
                logging.error(f"❌ خطأ API أثناء إنشاء رابط دعوة مشترك لقناة {channel_id}: {e}")
                return {"success": False, "invite_link": None, "error": str(e)}
        except Exception as e:
            logging.error(f"❌ خطأ غير متوقع أثناء إنشاء رابط دعوة مشترك للقناة {channel_id}: {e}")
            return {"success": False, "invite_link": None, "error": str(e)}

    logging.error(f"🚫 فشل إنشاء رابط دعوة مشترك لقناة {channel_id} بعد {max_retries} محاولات.")
    return {"success": False, "invite_link": None, "error": f"Failed after {max_retries} retries due to flood control."}

# ----------------- 🔹 إضافة المستخدم إلى القناة -----------------
# ✅ تعديل: إضافة `bot: Bot` كأول معامل
async def generate_channel_invite_link(bot: Bot, telegram_id: int, channel_id: int, channel_name: str):
    """
    توليد رابط دعوة لمستخدم لقناة محددة.
    """
    try:
        try:
            # ✅ تعديل: استخدام `bot` المستلم
            await bot.unban_chat_member(chat_id=channel_id, user_id=telegram_id)
            logging.info(f"Attempted to unban user {telegram_id} from channel {channel_id}.")
        except TelegramAPIError as e:
            logging.warning(f"⚠️ Could not unban user {telegram_id} from channel {channel_id}: {e.message}")

        # ✅ تعديل: استخدام `bot` المستلم
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=channel_id,
            creates_join_request=True,
            name=f"الرابط الدائم لـ {channel_name}"
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


# ✅ تعديل: إضافة `bot: Bot` كأول معامل
async def remove_user_from_channel(bot: Bot, connection, telegram_id: int, channel_id: int):
    """
    إزالة المستخدم من القناة وإرسال إشعار له.
    """
    try:
        channel_info = await connection.fetchrow(
            """SELECT stc.channel_name, st.name as subscription_type_name
            FROM subscription_type_channels stc
            JOIN subscription_types st ON stc.subscription_type_id = st.id
            WHERE stc.channel_id = $1 LIMIT 1""",
            channel_id
        )

        channel_display_name = channel_info['channel_name'] if channel_info and channel_info[
            'channel_name'] else f"القناة {channel_id}"
        subscription_type_name_for_message = channel_info['subscription_type_name'] if channel_info else "الاشتراك"

        try:
            # ✅ تعديل: استخدام `bot` المستلم
            await bot.ban_chat_member(chat_id=channel_id, user_id=telegram_id)
            logging.info(f"✅ تمت إزالة المستخدم {telegram_id} من القناة {channel_display_name} ({channel_id}).")

            # ✅ تعديل: استخدام `bot` المستلم
            await bot.unban_chat_member(
                chat_id=channel_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
            logging.info(f"User {telegram_id} unbanned from channel {channel_id} (if was banned).")
        except TelegramAPIError as e:
            logging.error(f"❌ فشل إزالة المستخدم {telegram_id} من القناة {channel_display_name} ({channel_id}): {e}")
            pass

        message_to_user = (
            f"⚠️ تم إخراجك من قناة '{channel_display_name}' (التابعة لاشتراك '{subscription_type_name_for_message}') بسبب انتهاء الاشتراك.\n"
            "🔄 يمكنك التجديد للعودة مجددًا!"
        )
        # ✅ تعديل: تمرير `bot` إلى الدالة الأخرى
        await send_message_to_user(bot, telegram_id, message_to_user)
        return True
    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع أثناء إزالة المستخدم {telegram_id} من القناة {channel_id}: {e}")
        return False


# utils/db_utils.py

import logging
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramNotFound,
)


async def remove_users_from_channel(bot: Bot, telegram_id: int, channel_id: int) -> bool:
    """
    Removes a user from a channel and sends them a notification.
    It safely skips owners and administrators.
    """
    logger = logging.getLogger(__name__)  # استخدام المسجل (Logger)

    message_text_template = (
        "🔔 تنبيه مهم\n\n"
        "تم الغاء اشتراكك وازالتك من {channel_display_name}\n"
        "لتتمكن من الانضمام مجددًا، يرجى تجديد اشتراكك."
    )

    try:
        # --- ✅ الخطوة 1: التحقق من رتبة المستخدم أولاً ---
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)

            if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
                logger.warning(
                    f"Skipping removal of user {telegram_id} from channel {channel_id} because they are an {member.status.value}."
                )
                return True  # نعتبر هذه العملية ناجحة لأننا تعاملنا معها بشكل صحيح
        except TelegramBadRequest as e:
            # حالة خاصة: إذا كان المستخدم غير موجود أصلاً في القناة
            if "user not found" in str(e).lower() or "participant_id_invalid" in str(e).lower():
                logger.warning(f"User {telegram_id} not found in channel {channel_id} to begin with. Skipping removal.")
                return True  # نعتبره نجاحاً لأنه ليس هناك ما نفعله
            else:
                raise e  # نرفع الأخطاء الأخرى من نوع BadRequest

        # --- الخطوة 2: إذا كان المستخدم عضواً عادياً، قم بالإزالة ---

        # تحسين الرسالة باسم القناة الفعلي
        channel_display_name = f"القناة (ID: {channel_id})"
        try:
            channel_info = await bot.get_chat(channel_id)
            title = getattr(channel_info, "title", None)
            if title:
                channel_display_name = f'"{title}"'
        except Exception as e_title:
            logger.warning(f"Could not get channel info for {channel_id} to get title: {e_title}")

        final_message_text = message_text_template.format(channel_display_name=channel_display_name)

        # عملية الطرد المؤقت
        logger.info(f"Attempting to ban user {telegram_id} from channel {channel_id}")
        await bot.ban_chat_member(
            chat_id=channel_id,
            user_id=telegram_id,
            revoke_messages=False,
        )
        logger.info(f"User {telegram_id} banned from channel {channel_id}.")

        logger.info(f"Attempting to unban user {telegram_id} to allow rejoining")
        await bot.unban_chat_member(
            chat_id=channel_id,
            user_id=telegram_id,
            only_if_banned=True,
        )
        logger.info(f"User {telegram_id} unbanned (if was banned).")

        # إرسال الإشعار
        logger.info(f"Sending notification to user {telegram_id}")
        await bot.send_message(chat_id=telegram_id, text=final_message_text)
        logger.info(f"Notification sent to user {telegram_id}.")

        return True

    except TelegramBadRequest as e:
        # معالجة خاصة للأخطاء التي قد تحدث رغم التحقق (حالات نادرة)
        if "can't remove chat owner" in str(e).lower() or "user is an administrator of the chat" in str(e).lower():
            logger.warning(f"Attempted to remove an admin/owner {telegram_id} despite check: {e}")
            return True  # نعتبره نجاحاً لتجنب تسجيل خطأ غير ضروري
        else:
            logger.error(f"Telegram bad request for {telegram_id} in {channel_id}: {e}", exc_info=True)
            return False

    except TelegramForbiddenError:
        # إذا كان البوت محظوراً من قبل المستخدم، لا يمكننا إرسال رسالة لكن الإزالة قد تكون نجحت
        logger.warning(f"User {telegram_id} has blocked the bot. Cannot send removal notification.")
        return True  # نعتبر الإزالة ناجحة

    except TelegramAPIError as e:
        logger.error(f"Telegram API error for user {telegram_id}, channel {channel_id}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error for user {telegram_id}, channel {channel_id}: {e}", exc_info=True)
        return False


# ----------------- 🔹 إغلاق جلسة بوت تيليجرام -----------------
# ✅ تعديل: إضافة `bot: Bot` كأول معامل
async def close_telegram_bot_session(bot: Bot):
    """
    إغلاق جلسة Telegram Bot API.
    """
    try:
        # ✅ تعديل: استخدام `bot` المستلم
        if bot and bot.session:
            await bot.session.close()
            logging.info("✅ تم إغلاق جلسة Telegram Bot API بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق جلسة Telegram Bot API: {e}")