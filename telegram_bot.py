# telegram_bot.py
import logging
import os
import asyncio
import sys
import json
import aiohttp  # ✅ استيراد `aiohttp` لإرسال الطلبات
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatJoinRequest
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from dotenv import load_dotenv
from quart import Blueprint, current_app, request, jsonify
from database.db_queries import get_subscription, add_user, get_user_db_id_by_telegram_id, \
    get_active_subscription_types, get_subscription_type_details_by_id, add_subscription_for_legacy, \
    add_pending_subscription
import asyncpg
from aiogram.enums import ChatMemberStatus
from functools import partial
from typing import Optional
from datetime import datetime, timezone, timedelta

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
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID"))

# ✅ التحقق من القيم المطلوبة في البيئة
if not TELEGRAM_BOT_TOKEN or not WEB_APP_URL or not SUBSCRIBE_URL or not WEBHOOK_SECRET:
    raise ValueError("❌ خطأ: تأكد من ضبط جميع المتغيرات البيئية!")

# 🔹 إعداد Aiogram 3.x
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# 🔹 إنشاء Blueprint لاستخدامه في `app.py`
telegram_bot_bp = Blueprint("telegram_bot", __name__)  # ✅ تغيير الاسم إلى `telegram_bot_bp`



async def check_if_legacy_migration_done(conn: asyncpg.Connection, user_db_id: int) -> bool:
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id = $1 AND source = 'legacy'",
        user_db_id
    )
    return count > 0


async def find_legacy_subscriptions_by_username(conn: asyncpg.Connection, username_clean: str):
    if not username_clean:
        return []
    return await conn.fetch(
        """SELECT ls.*, st.channel_id AS target_channel_id
           FROM legacy_subscriptions ls
           JOIN subscription_types st ON ls.subscription_type_id = st.id
           WHERE ls.username = $1 AND ls.processed = FALSE
           ORDER BY ls.id""",
        username_clean
    )


async def mark_legacy_subscription_processed(conn: asyncpg.Connection, legacy_sub_id: int):
    await conn.execute("UPDATE legacy_subscriptions SET processed = TRUE WHERE id = $1", legacy_sub_id)
    logging.info(f"Marked legacy subscription ID {legacy_sub_id} as processed.")


async def handle_legacy_user(
        conn: asyncpg.Connection,
        # bot: Bot, # لم تعد هناك حاجة لإرسال رسائل للمستخدم من هنا
        telegram_id: int,
        user_db_id: int,
        username_clean: str
):
    legacy_records = await find_legacy_subscriptions_by_username(conn, username_clean)
    migrated_count = 0
    if not legacy_records:
        logging.info(
            f"No unprocessed legacy records found for {username_clean} (UserDBID: {user_db_id}, TGID: {telegram_id}). Might have been processed or username mismatch.")
        return False

    for legacy_sub in legacy_records:
        channel_id_from_legacy = legacy_sub['target_channel_id']
        if not channel_id_from_legacy:
            logging.error(
                f"Legacy migration: Could not determine target_channel_id for legacy_sub ID {legacy_sub['id']} (UserDBID: {user_db_id}). subscription_type_id {legacy_sub['subscription_type_id']} might be invalid or inactive.")
            continue

        async with conn.transaction():
            try:
                existing_migrated_sub = await conn.fetchrow(
                    "SELECT id FROM subscriptions WHERE user_id = $1 AND channel_id = $2 AND source = 'legacy'",
                    user_db_id, channel_id_from_legacy
                )
                if existing_migrated_sub:
                    logging.info(
                        f"Legacy subscription for channel {channel_id_from_legacy} already migrated for UserDBID {user_db_id}. Marking original legacy record {legacy_sub['id']} as processed.")
                    await mark_legacy_subscription_processed(conn, legacy_sub['id'])
                    continue

                is_active_legacy = legacy_sub['expiry_date'] > datetime.now(timezone.utc) if legacy_sub[
                    'expiry_date'] else False

                await add_subscription_for_legacy(  # تأكد أن هذه هي الدالة المعدلة من db_queries.py
                    connection=conn,
                    user_id=user_db_id,
                    telegram_id=telegram_id,
                    channel_id=channel_id_from_legacy,
                    subscription_type_id=legacy_sub['subscription_type_id'],
                    start_date=legacy_sub['start_date'],  # معامل إجباري
                    expiry_date=legacy_sub['expiry_date'],  # معامل إجباري
                    subscription_plan_id=None,  # أو legacy_sub.get('subscription_plan_id')
                    is_active=is_active_legacy,
                    source='legacy'
                )
                await mark_legacy_subscription_processed(conn, legacy_sub['id'])
                migrated_count += 1
                logging.info(
                    f"Successfully migrated legacy subscription (ID: {legacy_sub['id']}) for UserDBID {user_db_id} to channel {channel_id_from_legacy}.")
            except Exception as e:
                logging.error(
                    f"Error migrating legacy subscription (ID: {legacy_sub['id']}) for UserDBID {user_db_id}: {e}",
                    exc_info=True)
    return migrated_count > 0


async def add_pending_subscription_fixed(  # تم تغيير الاسم للتوضيح
        connection: asyncpg.Connection,
        user_db_id: int,
        telegram_id: int,
        channel_id: int,
        subscription_type_id: int
) -> bool:
    """
    يضيف اشتراكًا معلقًا للمراجعة.
    يستخدم ON CONFLICT (telegram_id, channel_id) DO NOTHING.
    يعود True إذا تم إدراج صف جديد، False إذا لم يتم إدراج أي شيء (بسبب التعارض).
    """
    try:
        record_id = await connection.fetchval(
            """
            INSERT INTO pending_subscriptions (user_db_id, telegram_id, channel_id, subscription_type_id, status)
            VALUES ($1, $2, $3, $4, 'pending')
            ON CONFLICT (telegram_id, channel_id) DO NOTHING
            RETURNING id; 
            """,
            user_db_id,
            telegram_id,
            channel_id,
            subscription_type_id,
        )

        if record_id is not None:
            logging.info(
                f"Successfully added pending subscription with ID {record_id} for TGID {telegram_id}, channel {channel_id}.")
            return True
        else:
            logging.info(
                f"Pending subscription for TGID {telegram_id}, channel {channel_id} likely already exists (ON CONFLICT DO NOTHING triggered).")
            return False
    except Exception as e:
        logging.error(
            f"❌ Error in add_pending_subscription_fixed for user_db_id {user_db_id} (TG: {telegram_id}), channel {channel_id}: {e}",
            exc_info=True
        )
        return False


async def handle_telegram_list_user(
        conn: asyncpg.Connection,
        telegram_id: int,
        user_db_id: int,
        full_name: str,
        member_statuses: dict,
        # bot_instance,  # <-- لم نعد بحاجة لتمريره، سنستخدم `bot` العام
        admin_tg_id  # لا يزال يُمرر أو يمكن استخدام ADMIN_ID العام
):
    # admin_telegram_id = admin_tg_id # استخدام الوسيطة الممررة
    admin_telegram_id = ADMIN_ID  # استخدام العام
    added_to_pending_count = 0
    # افترض أن get_active_subscription_types معرفة ومتاحة
    active_subscription_types = await get_active_subscription_types(conn)

    non_active_member_statuses = [
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.RESTRICTED
    ]

    for sub_type in active_subscription_types:
        managed_channel_id = sub_type['channel_id']
        subscription_type_id = sub_type['id']
        channel_name = sub_type['name']
        member_status_obj = member_statuses.get(managed_channel_id)

        if member_status_obj and member_status_obj.status not in non_active_member_statuses:
            current_channel_subscription = await get_subscription(conn, telegram_id, managed_channel_id)

            if current_channel_subscription and current_channel_subscription.get('is_active', False):
                logging.info(
                    f"User TGID {telegram_id} has an ACTIVE subscription for channel {channel_name} ({managed_channel_id}). Skipping pending logic.")
                continue

            logging.info(
                f"User TGID {telegram_id} (Name: {full_name}) found in channel {channel_name} ({managed_channel_id}). Subscription status: {'INACTIVE' if current_channel_subscription else 'NOT FOUND'}. Needs review for pending.")

            status_of_pending_add = "الرجاء المراجعة واتخاذ الإجراء."
            existing_pending_sub = await conn.fetchrow(
                "SELECT id, status FROM pending_subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                telegram_id, managed_channel_id
            )

            if existing_pending_sub:
                logging.info(
                    f"User TGID {telegram_id} already in PENDING for channel {channel_name} with status '{existing_pending_sub['status']}'. No new entry will be added.")
                status_of_pending_add = f"موجود مسبقًا في قائمة الاشتراكات المعلقة (الحالة: {existing_pending_sub['status']})."
            else:
                try:
                    # استخدام الدالة المصححة
                    was_newly_added = await add_pending_subscription_fixed(
                        connection=conn,
                        user_db_id=user_db_id,
                        telegram_id=telegram_id,
                        channel_id=managed_channel_id,
                        subscription_type_id=subscription_type_id
                    )
                    if was_newly_added:
                        added_to_pending_count += 1
                        logging.info(f"User TGID {telegram_id} newly added to PENDING for channel {channel_name}.")
                        status_of_pending_add = "تمت إضافته حديثًا إلى قائمة الاشتراكات المعلقة للمراجعة."
                    else:
                        # هذا يعني أن add_pending_subscription_fixed أعادت False (بسبب ON CONFLICT أو خطأ آخر)
                        logging.error(
                            f"Failed to add TGID {telegram_id} to PENDING for channel {channel_name} (add_pending_subscription_fixed returned False). This means it likely already existed, or an error occurred within the function.")
                        status_of_pending_add = "لم تتم الإضافة إلى الاشتراكات المعلقة (قد يكون موجودًا مسبقًا أو حدث خطأ)."
                except asyncpg.exceptions.UniqueViolationError:  # هذا لا يجب أن يحدث إذا كان ON CONFLICT يعمل
                    logging.warning(
                        f"UniqueViolationError (should be handled by ON CONFLICT) for TGID {telegram_id}, PENDING for {channel_name}.")
                    status_of_pending_add = "خطأ تفرد غير متوقع (يفترض أن ON CONFLICT يعالجه)."
                except Exception as e:
                    logging.error(
                        f"EXCEPTION while adding to PENDING for TGID {telegram_id}, channel {channel_name}: {e}",
                        exc_info=True)
                    status_of_pending_add = f"حدث خطأ أثناء محاولة إضافته إلى `pending_subscriptions`: {str(e)}."

            # نرسل الإشعار فقط إذا لم يكن المستخدم موجودًا مسبقًا في pending بحالة 'pending'
            # أو إذا تمت إضافته حديثًا
            should_notify_admin = not (existing_pending_sub and existing_pending_sub['status'] == 'pending') or (
                        status_of_pending_add == "تمت إضافته حديثًا إلى قائمة الاشتراكات المعلقة للمراجعة.")

            if should_notify_admin:
                admin_message = (
                    f"👤 مراجعة مستخدم:\n"
                    f"الاسم: {full_name} (TG ID: `{telegram_id}`, DB ID: `{user_db_id}`)\n"
                    f"القناة: {channel_name} (`{managed_channel_id}`)\n"
                    f"الاشتراك الحالي: {'غير نشط' if current_channel_subscription else 'غير موجود'}\n"
                    f"الحالة بخصوص الإضافة للمعلقة: {status_of_pending_add}"
                )
                if admin_telegram_id:
                    try:
                        await bot.send_message(admin_telegram_id, admin_message,
                                               parse_mode="Markdown")  # استخدام `bot` العام
                    except Exception as e_admin_msg:
                        logging.error(f"Failed to send admin notification for user {telegram_id}: {e_admin_msg}")
                else:
                    logging.warning("ADMIN_ID not set. Cannot send admin notification.")

        elif member_status_obj:
            logging.info(
                f"User TGID {telegram_id} status in channel {channel_name} is '{member_status_obj.status}'. Skipping pending logic.")
        else:
            logging.warning(
                f"No member status could be determined for user TGID {telegram_id} in channel {channel_name} ({managed_channel_id}). Skipping.")
    return added_to_pending_count > 0


@dp.message(Command("start"))
async def start_command(message: types.Message):
    user = message.from_user
    telegram_id = user.id
    username_raw = user.username
    full_name = user.full_name or "مستخدم تيليجرام"
    username_clean = username_raw.lower().replace('@', '').strip() if username_raw else ""

    # bot_instance = bot # لم نعد بحاجة لهذا، سنستخدم `bot` مباشرة
    db_pool = current_app.db_pool  # انتبه: هذا يعتمد على أن current_app.db_pool معرف بشكل صحيح في سياق Quart.
    # إذا كان هذا الكود يعمل خارج سياق طلب Quart، ستحتاج لطريقة أخرى لتمرير db_pool.
    # admin_id_for_notifications = ADMIN_ID # يمكن استخدام ADMIN_ID مباشرة
    app_url_for_button = WEB_APP_URL

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await add_user(conn, telegram_id, username=username_raw, full_name=full_name)
            user_db_id = await get_user_db_id_by_telegram_id(conn, telegram_id)

            if not user_db_id:
                logging.error(f"Failed to get/create user_db_id for telegram_id {telegram_id}.")
                await message.answer("حدث خطأ أثناء معالجة طلبك. يرجى المحاولة لاحقًا أو التواصل مع الدعم.")
                return

            managed_channels = await get_active_subscription_types(conn)
            legacy_already_fully_migrated = await check_if_legacy_migration_done(conn, user_db_id)

            if username_clean and not legacy_already_fully_migrated:
                logging.info(
                    f"UserDBID {user_db_id} (TGID: {telegram_id}, User: {username_clean}) - Attempting legacy migration.")
                processed_this_time = await handle_legacy_user(conn, telegram_id, user_db_id, username_clean)
                if processed_this_time:
                    logging.info(f"Legacy migration successful for user {user_db_id}.")
                    legacy_already_fully_migrated = True

            member_statuses = {}
            is_member_any_managed_channel_actively = False

            non_active_member_statuses_for_start = [
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
                ChatMemberStatus.RESTRICTED
            ]

            if managed_channels:
                for channel_info in managed_channels:
                    try:
                        member_status = await bot.get_chat_member(chat_id=channel_info['channel_id'],
                                                                  user_id=telegram_id)  # استخدام `bot` العام
                        member_statuses[channel_info['channel_id']] = member_status
                        if member_status.status not in non_active_member_statuses_for_start:
                            is_member_any_managed_channel_actively = True
                    except TelegramAPIError as e:  # يجب استيراد TelegramAPIError
                        if "user not found" in e.message.lower() or "chat not found" in e.message.lower() or "bot is not a member" in e.message.lower():
                            logging.warning(
                                f"Could not get chat member status for user {telegram_id} in channel {channel_info['channel_id']}: {e.message}")
                        else:
                            logging.error(
                                f"Telegram API error getting chat member for user {telegram_id} in channel {channel_info['channel_id']}: {e}",
                                exc_info=True)
                        member_statuses[channel_info['channel_id']] = None
                    except Exception as e_gen:
                        logging.error(
                            f"Generic error getting chat member for user {telegram_id} in channel {channel_info['channel_id']}: {e_gen}",
                            exc_info=True)
                        member_statuses[channel_info['channel_id']] = None

            if is_member_any_managed_channel_actively and not legacy_already_fully_migrated:
                any_legacy_record_exists_for_username = False
                if username_clean:
                    any_legacy_record_exists_for_username = await conn.fetchval(
                        "SELECT 1 FROM legacy_subscriptions WHERE username = $1 LIMIT 1", username_clean
                    )

                if not any_legacy_record_exists_for_username:
                    logging.info(
                        f"UserDBID {user_db_id} (TGID: {telegram_id}) is an active member. No legacy record. Checking channels via handle_telegram_list_user.")
                    await handle_telegram_list_user(
                        conn, telegram_id, user_db_id, full_name, member_statuses,
                        # bot_instance=bot, # لم نعد نمرره
                        admin_tg_id=ADMIN_ID
                    )
                else:
                    logging.info(
                        f"UserDBID {user_db_id} (TGID: {telegram_id}) is member, but a legacy record exists. Skipping 'telegram_list'.")

    bot_user_info = await bot.get_me()  # جلب معلومات البوت
    bot_display_name = bot_user_info.username if bot_user_info and bot_user_info.username else "Exaado"

    # رسالة ترحيب ثابتة
    welcome_text = (
        # f"{final_welcome_message_intro}" # تم الإزالة
        f"👋 مرحبًا {full_name}!\n\n"  # استخدم full_name الذي تم تعيين قيمة افتراضية له
        f"مرحبًا بك في **@Exaado**  \n"
        "هنا يمكنك إدارة اشتراكاتك في قنواتنا بسهولة.\n\n"
        "نتمنى لك تجربة رائعة! 🚀"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق 🔹",
                              web_app=WebAppInfo(url=WEB_APP_URL))],
    ])
    await message.answer(text=welcome_text, reply_markup=keyboard, parse_mode="Markdown")


# إضافة معالج لطلبات الانضمام
@dp.chat_join_request()
async def handle_join_request(join_request: ChatJoinRequest):
    user_id = join_request.from_user.id
    requested_chat_id = join_request.chat.id
    username = join_request.from_user.username or "لا يوجد اسم مستخدم"
    full_name = join_request.from_user.full_name or "لا يوجد اسم كامل"

    logging.info(
        f"🔹 طلب انضمام جديد من المستخدم {user_id} (@{username} - {full_name}) إلى القناة {requested_chat_id} ({join_request.chat.title or 'بدون عنوان'})")

    try:
        async with current_app.db_pool.acquire() as connection:
            channel_data = await connection.fetchrow(
                """
                SELECT 
                    stc.subscription_type_id, 
                    stc.channel_name AS joined_channel_name,
                    st.channel_id AS main_channel_id_for_subscription,
                    st.name AS subscription_package_name
                FROM subscription_type_channels stc
                JOIN subscription_types st ON stc.subscription_type_id = st.id
                WHERE stc.channel_id = $1
                """,
                requested_chat_id
            )

            if not channel_data:
                logging.warning(
                    f"⚠️ القناة {requested_chat_id} غير معرفة في جدول subscription_type_channels. سيتم رفض الطلب.")
                await bot.decline_chat_join_request(chat_id=requested_chat_id, user_id=user_id)
                return

            main_channel_id_for_subscription_check = channel_data['main_channel_id_for_subscription']
            actual_joined_channel_name = channel_data[
                                             'joined_channel_name'] or join_request.chat.title or f"القناة {requested_chat_id}"
            # subscription_package_name = channel_data['subscription_package_name'] # يمكنك استخدام هذا إذا احتجت إليه

            logging.info(
                f"🔍 القناة المطلوبة {requested_chat_id} ({actual_joined_channel_name}) تابعة لنوع الاشتراك ID: {channel_data['subscription_type_id']}. القناة الرئيسية للتحقق من الاشتراك هي: {main_channel_id_for_subscription_check}")

            subscription = await get_subscription(connection, user_id, main_channel_id_for_subscription_check)

            if subscription and subscription.get('is_active', False):
                logging.info(
                    f"✅ تم العثور على اشتراك نشط للمستخدم {user_id} المرتبط بالباقة التي تشمل القناة {requested_chat_id}")

                try:
                    # محاولة رفع الحظر عن المستخدم (كإجراء احترازي) قبل الموافقة
                    try:
                        logging.info(
                            f"ℹ️ محاولة رفع الحظر عن المستخدم {user_id} من القناة {requested_chat_id} كإجراء احترازي.")
                        await bot.unban_chat_member(
                            chat_id=requested_chat_id,
                            user_id=user_id,
                            only_if_banned=True  # يحاول فقط إذا كان المستخدم محظورًا بالفعل
                        )
                        logging.info(f"🛡️ تمت محاولة رفع الحظر (إذا كان موجودًا) عن {user_id} في {requested_chat_id}.")
                    except Exception as unban_error:
                        # سجل الخطأ ولكن لا توقف العملية بالضرورة
                        logging.warning(
                            f"⚠️ خطأ أثناء محاولة رفع الحظر عن المستخدم {user_id} من القناة {requested_chat_id}: {unban_error}. سنستمر في محاولة قبول الطلب.")

                    # قبول طلب الانضمام
                    await bot.approve_chat_join_request(
                        chat_id=requested_chat_id,
                        user_id=user_id
                    )
                    logging.info(
                        f"👍 تم قبول طلب انضمام المستخدم {user_id} إلى القناة {requested_chat_id} ({actual_joined_channel_name})")

                    # إرسال رسالة ترحيبية
                    try:
                        message_text = (
                            f"🎉 تهانينا، {full_name}!\n"
                            f"تمت إضافتك بنجاح إلى قناة \"{actual_joined_channel_name}\".\n"
                            "نتمنى لك تجربة رائعة. 😊"
                        )
                        await bot.send_message(user_id, message_text)
                        logging.info(
                            f"✉️ تم إرسال رسالة ترحيبية للمستخدم {user_id} للانضمام إلى {actual_joined_channel_name}")
                    except Exception as e_msg:
                        logging.warning(
                            f"⚠️ لم يتم إرسال رسالة الترحيب للمستخدم {user_id} بعد قبوله في {actual_joined_channel_name}: {e_msg}")

                except Exception as e_approve:
                    logging.error(
                        f"❌ خطأ أثناء قبول طلب الانضمام للمستخدم {user_id} في القناة {requested_chat_id}: {e_approve}")
                    # في حال فشل القبول بشكل حرج، حاول الرفض كإجراء احتياطي
                    try:
                        await bot.decline_chat_join_request(chat_id=requested_chat_id, user_id=user_id)
                        logging.info(f"🛡️ تم رفض طلب الانضمام للمستخدم {user_id} بعد فشل محاولة القبول.")
                    except Exception as decline_fallback_error:
                        logging.error(f"❌ فشل رفض طلب الانضمام بعد فشل القبول: {decline_fallback_error}")
            else:
                logging.info(
                    f"🚫 لم يتم العثور على اشتراك نشط للمستخدم {user_id} يسمح بالانضمام إلى القناة {requested_chat_id} (التحقق تم عبر القناة الرئيسية {main_channel_id_for_subscription_check})")
                try:
                    await bot.decline_chat_join_request(
                        chat_id=requested_chat_id,
                        user_id=user_id
                    )
                    logging.info(
                        f"👎 تم رفض طلب انضمام المستخدم {user_id} للقناة {requested_chat_id} لعدم وجود اشتراك نشط.")
                except Exception as e_decline:
                    logging.error(
                        f"❌ خطأ أثناء رفض طلب الانضمام للمستخدم {user_id} في القناة {requested_chat_id}: {e_decline}")

    except Exception as e_general:
        logging.error(f"🚨 خطأ عام أثناء معالجة طلب الانضمام للمستخدم {user_id} للقناة {requested_chat_id}: {e_general}")
        try:
            await bot.decline_chat_join_request(chat_id=requested_chat_id, user_id=user_id)
            logging.info(f"🛡️ تم رفض طلب الانضمام للمستخدم {user_id} كإجراء احترازي بسبب خطأ عام.")
        except Exception as decline_error_general:
            logging.error(f"❌ فشل رفض طلب الانضمام بعد حدوث خطأ عام: {decline_error_general}")


# 🔹 وظيفة إدارة المستخدم (إضافة أو تحديث)
async def manage_user(connection, telegram_id, username=None, full_name=None):
    """
    إضافة مستخدم جديد أو تحديث بيانات مستخدم موجود
    """
    try:
        # البحث عن المستخدم الحالي
        async with current_app.db_pool.acquire() as connection:
            # البحث عن المستخدم الحالي
            existing_user = await connection.fetchrow(
                "SELECT id, username, full_name FROM users WHERE telegram_id = $1",
                telegram_id
            )

        if existing_user:
            # تحديث البيانات إذا كانت مختلفة
            update_needed = False
            current_username = existing_user['username']
            current_full_name = existing_user['full_name']

            if username and username != current_username:
                update_needed = True
            if full_name and full_name != current_full_name:
                update_needed = True

            if update_needed:
                await connection.execute("""
                    UPDATE users 
                    SET username = COALESCE($2, username),
                        full_name = COALESCE($3, full_name)
                    WHERE telegram_id = $1
                """, telegram_id, username, full_name)
                logging.info(f"✅ تم تحديث بيانات المستخدم {telegram_id}")

            return existing_user['id']
        else:
            # إضافة مستخدم جديد
            user_id = await connection.fetchval("""
                INSERT INTO users (telegram_id, username, full_name)
                VALUES ($1, $2, $3)
                RETURNING id
            """, telegram_id, username, full_name)
            logging.info(f"✅ تم إضافة مستخدم جديد {telegram_id} بـ ID: {user_id}")
            return user_id

    except Exception as e:
        logging.error(f"❌ خطأ في إدارة المستخدم {telegram_id}: {e}")
        return None


# 🔹 وظيفة تسجيل الدفعة الناجحة
async def record_successful_payment(
        user_db_id: int,
        telegram_id: int,
        plan_id: int,
        payment_id: str, # يستخدم كـ tx_hash
        payment_token: str,
        amount: float,
        username: Optional[str] = None,
        full_name: Optional[str] = None
):
    """
    تسجيل الدفعة الناجحة في جدول payments.
    يتم تعيين created_at و processed_at إلى الوقت الحالي (UTC+3) عند التسجيل.
    """
    try:
        async with current_app.db_pool.acquire() as connection:
            # التوقيت الحالي المحسوب في قاعدة البيانات (UTC+3)
            db_timestamp_expression = "(NOW() AT TIME ZONE 'UTC' + INTERVAL '3 hours')::timestamp"

            payment_record_id = await connection.fetchval(f"""
                INSERT INTO payments (
                    user_id,
                    telegram_id,
                    subscription_plan_id,
                    amount,
                    status,
                    currency,
                    payment_token,
                    tx_hash,
                    username,
                    full_name,
                    payment_method,
                    processed_at,  -- سيتم تعيينه بواسطة SQL
                    created_at     -- سيتم تعيينه بواسطة SQL
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    COALESCE($9, NULL),  -- username
                    COALESCE($10, NULL), -- full_name
                    $11,                 -- payment_method
                    {db_timestamp_expression}, -- processed_at
                    {db_timestamp_expression}  -- created_at
                )
                RETURNING id
            """,
                user_db_id,             # $1
                telegram_id,            # $2
                plan_id,                # $3
                amount,                 # $4
                'completed',            # $5 status
                'Stars',       # $6 currency
                payment_token,          # $7 payment_token
                payment_id,             # $8 tx_hash (using payment_id from Telegram)
                username,               # $9 username
                full_name,              # $10 full_name
                'Telegram stars'        # $11 payment_method
            )

            logging.info(f"✅ تم تسجيل الدفعة الناجحة برقم {payment_record_id}")
            return payment_record_id

    except Exception as e:
        logging.error(f"❌ خطأ في تسجيل الدفعة: {e}")
        return None

# 🔹 وظيفة معالجة الدفع الناجح مع آلية إعادة المحاولة
async def process_successful_payment_with_retry(
        telegram_id,
        plan_id,
        payment_id,
        payment_token,
        amount,
        full_name=None,
        username=None,
        max_retries=3
):
    """
    معالجة الدفع الناجح مع آلية إعادة المحاولة باستخدام current_app.db_pool
    """
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"🔄 محاولة معالجة الدفع {attempt}/{max_retries} للمستخدم {telegram_id}")

            # الاتصال بقاعدة البيانات باستخدام current_app.db_pool
            async with current_app.db_pool.acquire() as connection:
                # بدء المعاملة
                async with connection.transaction():
                    # 1. إدارة المستخدم (إضافة أو تحديث)
                    #    نمرر 'connection' المكتسبة لهذه الدالة
                    user_db_id = await manage_user(connection, telegram_id, username, full_name)
                    if not user_db_id:
                        raise Exception("فشل في إدارة بيانات المستخدم")

                    # 2. تسجيل الدفعة الناجحة
                    #    نمرر 'connection' المكتسبة لهذه الدالة
                    payment_record_id = await record_successful_payment(
                        user_db_id,
                        telegram_id,
                        plan_id,
                        payment_id,
                        payment_token,
                        amount,
                        username,
                        full_name
                    )

                    if not payment_record_id:
                        raise Exception("فشل في تسجيل الدفعة")

                    # 3. إرسال البيانات إلى API الاشتراك
                    #    هذه الدالة لا تحتاج إلى اتصال قاعدة بيانات
                    api_success = await send_payment_to_subscribe_api(
                        telegram_id=telegram_id,
                        plan_id=plan_id,
                        payment_id=payment_id,
                        payment_token=payment_token,
                        full_name=full_name or "غير معروف",
                        username=username or "غير معروف"
                    )

                    if not api_success:
                        raise Exception("فشل في إرسال البيانات إلى API الاشتراك")

                # إذا وصلت هنا، فالمعاملة تمت بنجاح (تم عمل commit تلقائياً)
                logging.info(f"✅ تم معالجة الدفع بنجاح للمستخدم {telegram_id}")
                return True

        except Exception as e:
            logging.error(f"❌ خطأ في المحاولة {attempt}/{max_retries} لمعالجة الدفع للمستخدم {telegram_id}: {str(e)}")

            try:
                async with current_app.db_pool.acquire() as error_conn:
                    await error_conn.execute("""
                        UPDATE payments 
                        SET status = 'failed', error_message = $1 
                        WHERE payment_token = $2 AND status = 'completed'
                    """, str(e), payment_token)
                    logging.info(f"⚠️ تم تحديث حالة الدفعة إلى 'failed' للمستخدم {telegram_id} بسبب: {str(e)}")
            except Exception as db_update_err:
                logging.error(f"❌ فشل في تحديث حالة الدفعة إلى 'failed' للمستخدم {telegram_id}: {db_update_err}")

        # انتظار قبل إعادة المحاولة (exponential backoff)
        if attempt < max_retries:
            wait_time = 2 ** attempt
            logging.info(f"⏳ انتظار {wait_time} ثانية قبل المحاولة التالية للمستخدم {telegram_id}...")
            await asyncio.sleep(wait_time)

    logging.critical(f"🚨 فشل جميع المحاولات لمعالجة الدفع للمستخدم {telegram_id}")
    return False


# 🔹 وظيفة معدلة لمعالجة الدفع الناجح
async def send_payment_to_subscribe_api(
        telegram_id: int,
        plan_id: int,
        payment_id: str,
        payment_token: str,
        full_name: str,
        username: str,
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
    """✅ معالجة الدفع الناجح مع التحسينات الجديدة"""
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
        full_name = payload.get("fullName") or message.from_user.full_name
        username = payload.get("telegramUsername") or message.from_user.username
        amount = payment.total_amount  # المبلغ بـ Telegram Stars

        # التحقق من البيانات الأساسية
        required_fields = [
            (telegram_id, "telegram_id"),
            (plan_id, "plan_id"),
            (payment_id, "payment_id"),
            (payment_token, "payment_token"),
            (amount, "amount")
        ]

        missing_fields = [name for value, name in required_fields if not value]
        if missing_fields:
            logging.error(f"❌ بيانات ناقصة: {', '.join(missing_fields)}")
            return

        # معالجة الدفع مع آلية إعادة المحاولة
        success = await process_successful_payment_with_retry(
            telegram_id=telegram_id,
            plan_id=plan_id,
            payment_id=payment_id,
            payment_token=payment_token,
            amount=amount,
            full_name=full_name,
            username=username,
            max_retries=3
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
