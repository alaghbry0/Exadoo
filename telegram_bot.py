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
from database.db_queries import get_subscription, add_user, get_user_db_id_by_telegram_id, get_active_subscription_types,get_subscription_type_details_by_id, add_subscription_for_legacy, add_pending_subscription
import asyncpg
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
        logging.info(f"No unprocessed legacy records found for {username_clean} (UserDBID: {user_db_id}, TGID: {telegram_id}). Might have been processed or username mismatch.")
        return False

    for legacy_sub in legacy_records:
        channel_id_from_legacy = legacy_sub['target_channel_id']
        if not channel_id_from_legacy:
            logging.error(f"Legacy migration: Could not determine target_channel_id for legacy_sub ID {legacy_sub['id']} (UserDBID: {user_db_id}). subscription_type_id {legacy_sub['subscription_type_id']} might be invalid or inactive.")
            continue

        async with conn.transaction():
            try:
                existing_migrated_sub = await conn.fetchrow(
                    "SELECT id FROM subscriptions WHERE user_id = $1 AND channel_id = $2 AND source = 'legacy'",
                    user_db_id, channel_id_from_legacy
                )
                if existing_migrated_sub:
                    logging.info(f"Legacy subscription for channel {channel_id_from_legacy} already migrated for UserDBID {user_db_id}. Marking original legacy record {legacy_sub['id']} as processed.")
                    await mark_legacy_subscription_processed(conn, legacy_sub['id'])
                    continue

                is_active_legacy = legacy_sub['expiry_date'] > datetime.now(timezone.utc) if legacy_sub['expiry_date'] else False

                await add_subscription_for_legacy( # تأكد أن هذه هي الدالة المعدلة من db_queries.py
                    connection=conn,
                    user_id=user_db_id,
                    telegram_id=telegram_id,
                    channel_id=channel_id_from_legacy,
                    subscription_type_id=legacy_sub['subscription_type_id'],
                    start_date=legacy_sub['start_date'], # معامل إجباري
                    expiry_date=legacy_sub['expiry_date'], # معامل إجباري
                    subscription_plan_id=None, # أو legacy_sub.get('subscription_plan_id')
                    is_active=is_active_legacy,
                    source='legacy'
                )
                await mark_legacy_subscription_processed(conn, legacy_sub['id'])
                migrated_count += 1
                logging.info(f"Successfully migrated legacy subscription (ID: {legacy_sub['id']}) for UserDBID {user_db_id} to channel {channel_id_from_legacy}.")
            except Exception as e:
                logging.error(f"Error migrating legacy subscription (ID: {legacy_sub['id']}) for UserDBID {user_db_id}: {e}", exc_info=True)
    return migrated_count > 0


async def handle_telegram_list_user(
        conn: asyncpg.Connection,
        telegram_id: int,
        user_db_id: int,
        full_name: str,
        member_statuses: dict
):
    admin_telegram_id = ADMIN_ID
    added_to_pending_count = 0
    active_subscription_types = await get_active_subscription_types(conn)

    for sub_type in active_subscription_types:
        managed_channel_id = sub_type['channel_id']
        subscription_type_id = sub_type['id']
        channel_name = sub_type['name']
        member_status = member_statuses.get(managed_channel_id)

        if member_status and member_status.status not in ["left", "kicked", "restricted", "banned"]:
            existing_actual_sub = await conn.fetchrow(
                "SELECT id, source FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2 LIMIT 1",
                telegram_id, managed_channel_id
            )
            if existing_actual_sub:
                logging.info(f"User TGID {telegram_id} has actual sub. Skipping.")
                continue

            # --- هنا يبدأ منطق الإشعار ---
            logging.info(
                f"User TGID {telegram_id} (Name: {full_name}) found in channel {channel_name} without active sub. Needs review.")

            status_of_pending_add = "الرجاء المراجعة واتخاذ الإجراء."  # رسالة افتراضية
            was_newly_added = False

            try:
                was_newly_added = await add_pending_subscription(
                    connection=conn,
                    user_db_id=user_db_id,
                    telegram_id=telegram_id,
                    channel_id=managed_channel_id,
                    subscription_type_id=subscription_type_id
                )

                if was_newly_added:
                    added_to_pending_count += 1
                    logging.info(f"User TGID {telegram_id} added to PENDING for channel {channel_name}.")
                    status_of_pending_add = "تمت إضافته إلى قائمة الاشتراكات المعلقة للمراجعة."
                else:
                    # التحقق مما إذا كان موجودًا بالفعل أم فشل لسبب آخر
                    is_already_pending = await conn.fetchval(
                        "SELECT 1 FROM pending_subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                        telegram_id, managed_channel_id
                    )
                    if is_already_pending:
                        logging.info(f"User TGID {telegram_id} already in PENDING for channel {channel_name}.")
                        status_of_pending_add = "موجود مسبقًا في قائمة الاشتراكات المعلقة."
                    else:
                        logging.error(
                            f"Failed to add TGID {telegram_id} to PENDING for channel {channel_name} (returned False, not found).")
                        status_of_pending_add = "فشلت محاولة إضافته إلى `pending_subscriptions` (لم يكن موجودًا مسبقًا)."

            except Exception as e:
                logging.error(f"EXCEPTION while processing PENDING for TGID {telegram_id}, channel {channel_name}: {e}",
                              exc_info=True)
                status_of_pending_add = f"حدث خطأ أثناء محاولة إضافته إلى `pending_subscriptions`: {str(e)}."

            # --- إرسال الإشعار الآن ---
            admin_message = (
                f"👤 مراجعة مستخدم:\n"
                f"الاسم: {full_name} (ID: `{telegram_id}`)\n"
                f"القناة: {channel_name} (`{managed_channel_id}`)\n"
                f"تم العثور عليه في القناة وليس لديه اشتراك مسجل.\n"
                f"الحالة بخصوص الإضافة للمعلقة: {status_of_pending_add}"
            )

            if admin_telegram_id:
                try:
                    await bot.send_message(admin_telegram_id, admin_message, parse_mode="Markdown")
                except Exception as e_admin_msg:
                    logging.error(f"Failed to send admin notification for user {telegram_id}: {e_admin_msg}")
            else:
                logging.warning("ADMIN_TELEGRAM_ID not set. Cannot send admin notification.")

    return added_to_pending_count > 0

# معالج أمر /start
# يجب تمرير الاعتماديات (bot, db_pool, web_app_url, admin_telegram_id) عند تسجيل هذا المعالج
@dp.message(Command("start"))
async def start_command(message: types.Message):
    user = message.from_user
    telegram_id = user.id
    username_raw = user.username
    full_name = user.full_name or "مستخدم تيليجرام"
    username_clean = username_raw.lower().replace('@', '').strip() if username_raw else ""


    async with current_app.db_pool.acquire() as conn:
        async with conn.transaction():
            await add_user(conn, telegram_id, username=username_raw, full_name=full_name)
            user_db_id = await get_user_db_id_by_telegram_id(conn, telegram_id)

            if not user_db_id:
                logging.error(f"Failed to get/create user_db_id for telegram_id {telegram_id}.")
                await message.answer("حدث خطأ أثناء معالجة طلبك. يرجى المحاولة لاحقًا أو التواصل مع الدعم.")
                return

            managed_channels = await get_active_subscription_types(conn)
            # if not managed_channels: # لا داعي للقلق هنا، سيتعامل الكود أدناه مع القائمة الفارغة
            #     logging.warning("No active subscription types (managed channels) found in the database.")

            legacy_already_fully_migrated = await check_if_legacy_migration_done(conn, user_db_id)
            # processed_legacy_this_time = False # لم نعد بحاجة لتتبع هذا لإرسال رسالة للمستخدم

            if username_clean and not legacy_already_fully_migrated:
                logging.info(f"UserDBID {user_db_id} (TGID: {telegram_id}, User: {username_clean}) - Attempting legacy migration.")
                processed_this_time = await handle_legacy_user(conn, telegram_id, user_db_id, username_clean) # أزلت bot من الوسائط
                if processed_this_time:
                    # user_message_parts.append("✅ تم تحديث بيانات اشتراكك السابق بنجاح!") # تم الإزالة
                    logging.info(f"Legacy migration successful for user {user_db_id}.") # سجل داخلي فقط
                    legacy_already_fully_migrated = True # مهم للمنطق التالي

            member_statuses = {}
            is_member_any_managed_channel = False
            if managed_channels: # فقط إذا كانت هناك قنوات مُدارة
                for channel_info in managed_channels:
                    try:
                        member_status = await bot.get_chat_member(chat_id=channel_info['channel_id'], user_id=telegram_id)
                        member_statuses[channel_info['channel_id']] = member_status
                        if member_status.status not in ["left", "kicked", "restricted", "banned"]:
                            is_member_any_managed_channel = True
                    except TelegramAPIError as e:
                        if "user not found" in e.message.lower() or "chat not found" in e.message.lower() or "bot is not a member" in e.message.lower():
                            logging.warning(f"Could not get chat member status for user {telegram_id} in channel {channel_info['channel_id']}: {e.message}")
                        else:
                            logging.error(f"Telegram API error getting chat member for user {telegram_id} in channel {channel_info['channel_id']}: {e}", exc_info=True)
                        member_statuses[channel_info['channel_id']] = None
                    except Exception as e_gen:
                        logging.error(f"Generic error getting chat member for user {telegram_id} in channel {channel_info['channel_id']}: {e_gen}", exc_info=True)
                        member_statuses[channel_info['channel_id']] = None

            active_subs_count = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = $1 AND is_active = TRUE AND expiry_date > NOW()",
                user_db_id
            )

            if is_member_any_managed_channel and not legacy_already_fully_migrated and active_subs_count == 0:
                any_legacy_record_exists_for_username = False
                if username_clean:
                    any_legacy_record_exists_for_username = await conn.fetchval(
                        "SELECT 1 FROM legacy_subscriptions WHERE username = $1 LIMIT 1", username_clean
                    )
                if not any_legacy_record_exists_for_username:
                    logging.info(f"UserDBID {user_db_id} (TGID: {telegram_id}) is member, no active subs, no legacy. Handling as 'telegram_list'.")
                    await handle_telegram_list_user( # لم نعد نهتم بالقيمة المرجعة هنا للرسالة
                        conn,   telegram_id, user_db_id, full_name, member_statuses
                    )
                    # if handled_as_telegram_list: # تم الإزالة
                        # user_message_parts.append("ℹ️ تم التعرف على عضويتك في إحدى قنواتنا. سيقوم المسؤول بمراجعة حالة اشتراكك قريبًا.")
                else:
                    logging.info(f"UserDBID {user_db_id} (TGID: {telegram_id}) is member, no active subs, but a legacy record (possibly processed) exists for username '{username_clean}'. Skipping 'telegram_list'.")

            # لا حاجة لهذا الجزء إذا كانت الرسالة ثابتة
            # if not user_message_parts:
            #     if active_subs_count > 0 and not processed_legacy_this_time:
            #          user_message_parts.append("✨ أهلاً بعودتك! اشتراكاتك الحالية نشطة.")

    # --- نهاية منطق قاعدة البيانات والمعاملة ---

    # بناء الرسالة النهائية - تم التبسيط
    # final_welcome_message_intro = "\n".join(user_message_parts) # تم الإزالة
    # if final_welcome_message_intro: # تم الإزالة
    #     final_welcome_message_intro += "\n\n---\n\n" # تم الإزالة

    # رسالة ترحيب ثابتة
    welcome_text = (
        # f"{final_welcome_message_intro}" # تم الإزالة
        f"👋 مرحبًا {full_name}!\n\n" # استخدم full_name الذي تم تعيين قيمة افتراضية له
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
    """
    معالجة طلبات الانضمام للقناة والتحقق من الاشتراكات
    """
    user_id = join_request.from_user.id
    chat_id = join_request.chat.id
    username = join_request.from_user.username or "لا يوجد اسم مستخدم"
    full_name = join_request.from_user.full_name or "لا يوجد اسم كامل"

    logging.info(f"🔹 طلب انضمام جديد من المستخدم {user_id} (@{username} - {full_name}) إلى القناة {chat_id}")

    try:
        async with current_app.db_pool.acquire() as connection:
            # البحث عن اشتراك نشط للمستخدم في هذه القناة
            subscription = await get_subscription(connection, user_id, chat_id)

            if subscription:
                logging.info(f" تم العثور على اشتراك نشط للمستخدم {user_id} في القناة {chat_id}")

                # جلب اسم القناة من جدول subscription_types باستخدام channel_id أو subscription_type_id حسب التصميم
                # في هذا المثال يتم استخدام channel_id للبحث عن اسم القناة:
                subscription_type = await connection.fetchrow(
                    "SELECT name FROM subscription_types WHERE channel_id = $1", chat_id
                )
                channel_name = subscription_type['name'] if subscription_type else "القناة"

                # إذا كان المستخدم يملك اشتراك نشط، قبول طلب الانضمام
                try:
                    await bot.approve_chat_join_request(
                        chat_id=chat_id,
                        user_id=user_id
                    )
                    logging.info(f" تم قبول طلب انضمام المستخدم {user_id} إلى القناة {chat_id}")

                    # إرسال رسالة ترحيبية للمستخدم مع تضمين اسم القناة
                    try:
                        message_text = (

                            f"تهانينا، تم اضافتك إلى قناة {channel_name} بنجاح.🥳\n"
                            "نتمنى لك تجربه رائعه."
                        )
                        await bot.send_message(user_id, message_text)
                        logging.info(f" تم إرسال رسالة ترحيبية للمستخدم {user_id}")
                    except Exception as e:
                        logging.warning(f" لم يتم إرسال رسالة الترحيب للمستخدم {user_id}: {e}")
                except Exception as e:
                    logging.error(f" خطأ أثناء قبول طلب الانضمام للمستخدم {user_id}: {e}")
            else:
                logging.info(f" لم يتم العثور على اشتراك نشط للمستخدم {user_id} في القناة {chat_id}")
                # إذا لم يكن هناك اشتراك نشط، رفض طلب الانضمام دون إرسال رسالة للمستخدم
                try:
                    await bot.decline_chat_join_request(
                        chat_id=chat_id,
                        user_id=user_id
                    )
                    logging.info(f" تم رفض طلب انضمام المستخدم {user_id} لعدم وجود اشتراك نشط")
                except Exception as e:
                    logging.error(f" خطأ أثناء رفض طلب الانضمام للمستخدم {user_id}: {e}")

    except Exception as e:
        logging.error(f" خطأ أثناء معالجة طلب الانضمام للمستخدم {user_id}: {e}")

        # في حالة حدوث خطأ، يمكن رفض الطلب كإجراء احترازي
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as decline_error:
            logging.error(f" فشل رفض طلب الانضمام بعد حدوث خطأ: {decline_error}")


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