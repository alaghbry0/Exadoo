import logging
import os
import asyncio
import sys
import json
from decimal import Decimal
import aiohttp  # ✅ استيراد `aiohttp` لإرسال الطلبات
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatJoinRequest
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from dotenv import load_dotenv
from quart import Blueprint, current_app, request, jsonify
from database.db_queries import get_subscription, upsert_user, get_user_db_id_by_telegram_id, \
    get_active_subscription_types, get_subscription_type_details_by_id, add_subscription_for_legacy, \
    add_pending_subscription,  record_telegram_stars_payment, record_payment
from routes.subscriptions import process_subscription_renewal
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


# 🔹 إزالة Webhook تمامًا قبل تشغيل Polling
async def remove_webhook():
    """🔄 إزالة Webhook حتى يعمل Polling"""
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ تم إزالة Webhook بنجاح!")


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
    """
    يعالج أمر /start.
    يقوم بتحديث بيانات المستخدم في قاعدة البيانات ويرسل رسالة ترحيب مع زر لتطبيق الويب.
    """
    user = message.from_user
    telegram_id = user.id
    # تأكد من أن اسم المستخدم قد يكون None
    username = user.username
    # توفير قيمة افتراضية للاسم الكامل إذا كان فارغًا
    full_name = user.full_name or "مستخدم تيليجرام"

    # الحصول على اتصال قاعدة البيانات
    # ملاحظة: تأكد من أن `current_app.db_pool` متاح في هذا السياق.
    try:
        db_pool = current_app.db_pool
    except (NameError, AttributeError):
        logging.error("db_pool is not defined or accessible via current_app.")
        await message.answer("حدث خطأ فني في البوت. يرجى المحاولة لاحقاً.")
        return

    # 1. تحديث بيانات المستخدم في قاعدة البيانات (UPSERT)
    async with db_pool.acquire() as conn:
        success = await upsert_user(conn, telegram_id, username, full_name)
        if not success:
            # إذا فشلت عملية قاعدة البيانات، أبلغ المستخدم وتوقف
            logging.error(f"Failed to upsert user with telegram_id {telegram_id}.")
            await message.answer("حدث خطأ أثناء تسجيل بياناتك. يرجى المحاولة مرة أخرى.")
            return

    # 2. إرسال رسالة الرد
    # يمكنك استخدام user.full_name مباشرة هنا أيضاً
    welcome_text = (
        f"👋 مرحبًا بك {hbold(full_name)}!\n\n"
        "أهلاً بك في بوت **@Exaado**، حيث يمكنك إدارة اشتراكاتك في قنواتنا بسهولة.\n\n"
        "نتمنى لك تجربة رائعة! 🚀"
    )

    # إنشاء زر تطبيق الويب
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 فتح التطبيق 🔹",
                              web_app=WebAppInfo(url=WEB_APP_URL))],
    ])

    # إرسال الرسالة مع الزر
    await message.answer(
        text=welcome_text,
        reply_markup=keyboard,
        parse_mode="HTML" # تم التغيير إلى HTML ليتوافق مع hbold
    )


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


# ==============================================================================
# 🌟 الدالة الوسيطة الجديدة لمعالجة دفع النجوم 🌟
# ==============================================================================
async def process_stars_payment_and_renew(bot: Bot, payment_details: dict):
    """
    تتولى هذه الدالة تسجيل دفعة النجوم ثم استدعاء محرك التجديد الموحد.
    تحتوي على آلية إعادة المحاولة الخاصة بها لضمان الموثوقية.
    """
    telegram_id = payment_details['telegram_id']
    payment_token = payment_details['payment_token']
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(
                f"🔄 [Stars] Attempt {attempt}/{max_retries} to process payment for user={telegram_id}, token={payment_token}")

            async with current_app.db_pool.acquire() as connection:
                async with connection.transaction():
                    # الخطوة 1: تسجيل الدفعة في جدول payments باستخدام دالة موحدة.
                    payment_record = await record_payment(
                        conn=connection,
                        telegram_id=telegram_id,
                        subscription_plan_id=payment_details['plan_id'],
                        amount=Decimal(payment_details['amount']),
                        payment_token=payment_token,
                        status='pending',
                        payment_method='Telegram Stars', # <-- استخدام payment_method
                        currency='Stars',
                        tx_hash=payment_details['payment_id'],
                        username=payment_details['username'],
                        full_name=payment_details['full_name']
                    )

                    if not payment_record:
                        raise Exception("Failed to record initial pending payment for Telegram Stars.")

                    # الخطوة 2: تجهيز البيانات لدالة التجديد (لا تغيير هنا)
                    payment_data_for_renewal = {
                        **payment_record,
                        "tx_hash": payment_record['tx_hash'],
                        "amount_received": payment_record['amount_received']
                    }

                    # الخطوة 3: استدعاء محرك التجديد الموحد
                    await process_subscription_renewal(
                        connection=connection,
                        bot=bot,
                        payment_data=payment_data_for_renewal
                    )

            logging.info(f"✅ [Stars] Successfully handed over payment for user={telegram_id} to renewal system.")
            return

        except Exception as e:
            logging.error(f"❌ [Stars] Error in attempt {attempt}/{max_retries} for user={telegram_id}: {e}", exc_info=True)
            if attempt < max_retries:
                wait_time = 2 ** attempt
                logging.info(f"⏳ [Stars] Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

    # --- فشلت كل المحاولات ---
    logging.critical(f"🚨 [Stars] All attempts failed for user={telegram_id}, token={payment_token}. Manual check required.")
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚨 فشل حرج في معالجة دفعة نجوم!\n\nUser ID: `{telegram_id}`\nToken: `{payment_token}`\n\nيرجى المراجعة اليدوية.",
                parse_mode="Markdown"
            )
        except Exception as notify_err:
            logging.error(f"Failed to send critical failure notification to admin: {notify_err}")


# ==============================================================================
# 📥 معالج الدفع الناجح (مع تحسين التحقق) 📥
# ==============================================================================
@dp.message(lambda message: message.successful_payment is not None)
async def handle_successful_payment(message: types.Message, bot: Bot):
    """
    يعالج رسالة الدفع الناجح، يستخرج البيانات، ويسلمها للمعالج الجديد.
    """
    payment = message.successful_payment
    try:
        logging.info(f"📥 [Stars] Received successful payment from user={message.from_user.id}")
        payload = json.loads(payment.invoice_payload)

        payment_details = {
            "telegram_id": payload.get("userId"),
            "plan_id": payload.get("planId"),
            "payment_id": payment.telegram_payment_charge_id,
            "payment_token": payload.get("paymentToken"),
            "amount": payment.total_amount,
            "full_name": payload.get("fullName") or message.from_user.full_name,
            "username": payload.get("telegramUsername") or message.from_user.username
        }

        # --- تحقق محسن من الحقول الإلزامية ---
        required_keys = ["telegram_id", "plan_id", "payment_id", "payment_token", "amount"]
        if not all(payment_details.get(key) for key in required_keys):
            logging.error(f"❌ [Stars] Missing mandatory data in payment details: {payment_details}")
            # يمكنك إرسال رسالة للمستخدم هنا إذا أردت
            await message.reply("عذرًا، حدث خطأ في معالجة دفعتك بسبب بيانات ناقصة. يرجى التواصل مع الدعم.")
            return

        asyncio.create_task(process_stars_payment_and_renew(bot, payment_details))

    except json.JSONDecodeError as e:
        logging.error(f"❌ [Stars] Invalid JSON in invoice_payload: {e}")
    except Exception as e:
        logging.error(f"❌ [Stars] Unexpected error in handle_successful_payment: {e}", exc_info=True)


# ==============================================================================
# 🧐 معالج التحقق المسبق (لا يتطلب تغيير) 🧐
# ==============================================================================
@dp.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: types.PreCheckoutQuery, bot: Bot):
    """التحقق من صحة الفاتورة قبل إتمام الدفع"""
    try:
        payload = json.loads(pre_checkout.invoice_payload)
        if not payload.get("userId") or not payload.get("planId"):
            logging.error("❌ `invoice_payload` is invalid in pre_checkout!")
            await bot.answer_pre_checkout_query(pre_checkout.id, ok=False, error_message="بيانات الدفع غير صالحة!")
            return

        await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)
        logging.info(f"✅ [Stars] Pre-checkout approved for user={pre_checkout.from_user.id}")

    except Exception as e:
        logging.error(f"❌ Error in pre_checkout_query: {e}")
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
        sys.exit(1)
