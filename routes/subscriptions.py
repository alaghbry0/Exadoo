# routes/subscriptions.py

import logging
import pytz
import os
import json
import asyncio  # <-- إضافة جديدة لآلية إعادة المحاولة
from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta, timezone
from database.db_queries import (
    get_user,
    add_user,
    add_subscription,
    update_subscription,
    add_scheduled_task,
    update_payment_with_txhash  # <-- إضافة مهمة لتحديث حالة الدفع
)
from typing import Optional
from decimal import Decimal
from utils.db_utils import generate_channel_invite_link, send_message_to_user
from asyncpg import Connection
from aiogram import Bot
from utils.notifications import create_notification

# --- إعدادات وثوابت ---
LOCAL_TZ = pytz.timezone("Asia/Riyadh")
IS_DEVELOPMENT = True

# --- ثوابت جديدة للتحكم في إعادة المحاولة ---
SUBSCRIPTION_RENEWAL_RETRIES = 3  # عدد محاولات التجديد
SUBSCRIPTION_RENEWAL_RETRY_DELAY = 10  # الثواني بين كل محاولة


# --- الدوال المساعدة (تبقى كما هي) ---

async def calculate_subscription_dates(connection: Connection, telegram_id: int, main_channel_id: int,
                                       duration_days: int, duration_minutes_dev: int,
                                       current_time_utc: datetime) -> tuple[datetime, datetime]:
    """Helper function to calculate start and expiry dates."""
    existing_main_channel_sub = await connection.fetchrow(
        "SELECT id, start_date, expiry_date, is_active FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
        telegram_id, main_channel_id
    )

    start_date = current_time_utc
    base_expiry = current_time_utc

    if existing_main_channel_sub and \
            existing_main_channel_sub['is_active'] and \
            existing_main_channel_sub['expiry_date'] >= current_time_utc:
        start_date = existing_main_channel_sub['start_date']
        base_expiry = existing_main_channel_sub['expiry_date']

    new_expiry_date = base_expiry + timedelta(days=duration_days, minutes=duration_minutes_dev)
    return start_date, new_expiry_date


# ==============================================================================
# 🌟 الدالة الرئيسية الجديدة (Wrapper Function) 🌟
# ==============================================================================

async def process_subscription_renewal(
        connection: Connection,
        bot: Bot,
        payment_data: dict,
) -> tuple[bool, str]:
    """
    الدالة الأساسية التي تدير عملية تجديد الاشتراك مع آلية إعادة المحاولة.
    وهي مسؤولة عن تحديث حالة الدفع النهائية (completed أو failed).
    """
    telegram_id = payment_data.get("telegram_id")
    payment_token = payment_data.get("payment_token")
    tx_hash = payment_data.get("tx_hash")

    success = False
    message = "فشل تفعيل الاشتراك بعد عدة محاولات."

    # --- آلية إعادة المحاولة ---
    for attempt in range(1, SUBSCRIPTION_RENEWAL_RETRIES + 1):
        try:
            logging.info(
                f"🔄 [Renewal Attempt {attempt}/{SUBSCRIPTION_RENEWAL_RETRIES}] for user={telegram_id}, token={payment_token}")

            # استدعاء دالة المنطق الفعلي داخل transaction لضمان سلامة البيانات
            async with connection.transaction():
                renewal_success, renewal_message = await _execute_renewal_logic(
                    connection=connection,
                    bot=bot,
                    payment_data=payment_data
                )

            if renewal_success:
                success = True
                message = renewal_message
                logging.info(f"✅ [Renewal Success] Subscription activated for user={telegram_id} on attempt {attempt}.")
                break  # اخرج من الحلقة عند النجاح
            else:
                message = renewal_message
                logging.warning(f"⚠️ [Renewal Attempt {attempt} Failed] for user={telegram_id}. Reason: {message}")

        except Exception as e:
            # هذا يلتقط الأخطاء الفادحة التي قد تحدث خارج _execute_renewal_logic
            logging.error(f"❌ [Renewal Attempt {attempt} Critical Error] for user={telegram_id}: {e}", exc_info=True)
            message = f"خطأ فادح في نظام التجديد: {e}"

        if not success and attempt < SUBSCRIPTION_RENEWAL_RETRIES:
            logging.info(f"⏳ Retrying in {SUBSCRIPTION_RENEWAL_RETRY_DELAY} seconds...")
            await asyncio.sleep(SUBSCRIPTION_RENEWAL_RETRY_DELAY)

    # --- الخطوة النهائية: تحديث حالة الدفع بناءً على النتيجة ---
    try:
        final_status = "completed" if success else "failed"
        final_error_message = None if success else f"Renewal Failed After Retries: {message}"

        await update_payment_with_txhash(
            conn=connection,
            payment_token=payment_token,
            tx_hash=tx_hash,
            amount_received=payment_data['amount_received'],
            status=final_status,  # <-- تحديث الحالة بناءً على النجاح أو الفشل
            error_message=final_error_message
        )
        logging.info(f"✅ [Payment Finalized] Payment token={payment_token} status set to '{final_status}'.")

    except Exception as e:
        logging.critical(
            f"CRITICAL ❌ [Payment Finalization Failed] Could not update payment status for token={payment_token}: {e}",
            exc_info=True)
        # هذه مشكلة خطيرة، يجب مراقبتها
        return False, "فشل حرج في تحديث سجل الدفع النهائي."

    return success, message


async def _activate_or_renew_subscription_core(
        connection: Connection,
        bot: Bot,
        telegram_id: int,
        subscription_type_id: int,
        duration_days: int,
        source: str,
        subscription_plan_id: Optional[int] = None,
        plan_name: Optional[str] = "اشتراك مخصص",
        payment_token: Optional[str] = None,
        tx_hash: Optional[str] = None,
        user_full_name: Optional[str] = None,
        user_username: Optional[str] = None,
        amount_received: Optional[Decimal] = None

) -> tuple[bool, str, dict]:
    """
    الدالة الجوهرية والمركزية لتفعيل أو تجديد أي اشتراك.
    Returns: (success, message, result_data)
    """
    try:
        # --- 1. جلب البيانات الأساسية ---
        user_record = await get_user(connection, telegram_id)
        if not user_record:
            # إذا لم يكن المستخدم موجودا، قم بإضافته بالبيانات المتاحة
            await add_user(connection, telegram_id, username=user_username, full_name=user_full_name)
            user_record = await get_user(connection, telegram_id)

        # استخدم الأسماء الممررة أو تلك الموجودة في قاعدة البيانات
        full_name = user_full_name or user_record.get('full_name')
        username = user_username or user_record.get('username')
        greeting_name = full_name or username or str(telegram_id)

        type_info = await connection.fetchrow(
            "SELECT name, channel_id AS main_channel_id FROM subscription_types WHERE id = $1",
            subscription_type_id
        )
        if not type_info or not type_info["main_channel_id"]:
            raise ValueError(f"نوع اشتراك غير مهيأ بقناة رئيسية: {subscription_type_id}")

        subscription_type_name = type_info['name']
        main_channel_id = int(type_info["main_channel_id"])

        all_channels = await connection.fetch(
            "SELECT channel_id, channel_name, is_main, invite_link FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC",
            subscription_type_id
        )
        main_channel_data = next((ch for ch in all_channels if ch['is_main']), None)
        if not main_channel_data or not main_channel_data.get('invite_link'):
            raise ValueError(f"رابط الدعوة للقناة الرئيسية لنوع الاشتراك {subscription_type_id} غير موجود.")

        main_invite_link = main_channel_data['invite_link']

        # --- 2. منطق تثبيت السعر (إذا كانت البيانات متاحة) ---
        if subscription_plan_id and amount_received is not None:
            await _handle_price_lock_in(
                connection=connection,
                telegram_id=telegram_id,
                plan_id=subscription_plan_id,
                subscription_type_id=subscription_type_id,
                amount_received=amount_received
            )

        # --- 3. حساب تواريخ البدء والانتهاء ---
        current_time_utc = datetime.now(timezone.utc)
        start_date, expiry_date = await calculate_subscription_dates(
            connection, telegram_id, main_channel_id, duration_days,
            120 if IS_DEVELOPMENT else 0, current_time_utc
        )

        # --- 4. إضافة أو تحديث الاشتراك الرئيسي ---
        existing_sub = await connection.fetchrow(
            "SELECT id FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2", telegram_id, main_channel_id)

        main_subscription_id = None
        if existing_sub:
            await update_subscription(
                connection=connection, telegram_id=telegram_id, channel_id=main_channel_id,
                subscription_type_id=subscription_type_id, new_expiry_date=expiry_date,
                start_date=start_date, is_active=True, subscription_plan_id=subscription_plan_id,
                payment_id=tx_hash, source=source, payment_token=payment_token
            )
            main_subscription_id = existing_sub['id']
        else:
            main_subscription_id = await add_subscription(
                connection=connection, telegram_id=telegram_id, channel_id=main_channel_id,
                subscription_type_id=subscription_type_id, start_date=start_date, expiry_date=expiry_date,
                is_active=True, subscription_plan_id=subscription_plan_id,
                payment_id=tx_hash, source=source, payment_token=payment_token, returning_id=True
            )

        if not main_subscription_id:
            raise RuntimeError("فشل في إنشاء أو تحديث سجل الاشتراك الرئيسي.")

        # --- 5. جدولة مهام القنوات الفرعية ---
        secondary_links_to_send = []
        for channel in all_channels:
            if not channel['is_main']:
                if channel.get('invite_link'):
                    secondary_links_to_send.append(
                        f"▫️ قناة <a href='{channel['invite_link']}'>{channel['channel_name']}</a>")
                    await add_scheduled_task(
                        connection=connection, task_type="remove_user", telegram_id=telegram_id,
                        execute_at=expiry_date, channel_id=channel['channel_id'], clean_up=True
                    )
                else:
                    logging.warning(
                        f"CORE: Skipping secondary channel {channel['channel_id']} for user {telegram_id} due to missing invite link.")

        # --- 6. تسجيل في سجل الاشتراكات ---
        previous_history = await connection.fetchval("SELECT 1 FROM subscription_history WHERE payment_id = $1",
                                                     tx_hash)
        # تحديد نوع الإجراء بناءً على المصدر ووجود اشتراك سابق
        if source.startswith('admin'):
            action_type = 'ADMIN_RENEWAL' if existing_sub else 'ADMIN_NEW'
        else:
            action_type = 'RENEWAL' if previous_history or existing_sub else 'NEW'

        history_data = json.dumps({"full_name": full_name, "username": username, "source": source})
        history_record = await connection.fetchrow(
            """INSERT INTO subscription_history 
               (subscription_id, invite_link, action_type, subscription_type_name, subscription_plan_name, 
                renewal_date, expiry_date, telegram_id, extra_data, payment_id, payment_token) 
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id""",
            main_subscription_id, main_invite_link, action_type, subscription_type_name,
            plan_name, start_date, expiry_date, telegram_id, history_data,
            tx_hash, payment_token
        )
        history_id = history_record['id'] if history_record else None

        # --- 7. إرسال الإشعارات ---
        # هذا هو الاشعار الموحد الذي طلبته
        action_verb = 'تجديد' if 'RENEWAL' in action_type else 'تفعيل'
        notification_title = f"{action_verb} اشتراك: {subscription_type_name}"
        notification_message = (
            f"🎉 تم بنجاح {action_verb.lower()} اشتراكك في \"{subscription_type_name}\"!\n"
            f"صالح حتى: {expiry_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}."
        )
        notification_extra = {
            "history_id": history_id,
            "main_invite_link": main_invite_link,
            "payment_token": payment_token
        }
        await create_notification(
            connection=connection, notification_type="subscription_update",
            title=notification_title, message=notification_message,
            extra_data=notification_extra, is_public=False, telegram_ids=[telegram_id]
        )

        if secondary_links_to_send:
            secondary_msg = (
                    f"📬 بالإضافة إلى اشتراكك الرئيسي، يمكنك الانضمام للقنوات الفرعية التالية:\n\n" +
                    "\n".join(secondary_links_to_send) +
                    "\n\n💡 اضغط على الرابط لتقديم طلب انضمام، وسيتم قبولك تلقائياً."
            )
            await send_message_to_user(bot, telegram_id, secondary_msg)

        logging.info(f"✅ CORE: Subscription {action_type} for user {telegram_id} processed successfully.")

        result_data = {
            "new_expiry_date": expiry_date.astimezone(LOCAL_TZ),
            "greeting_name": greeting_name,
            "subscription_type_name": subscription_type_name,
            "action_verb": action_verb
        }
        return True, "Subscription processed successfully", result_data

    except Exception as e:
        logging.error(f"❌ CORE: Error in _activate_or_renew_subscription_core for user {telegram_id}: {e}",
                      exc_info=True)
        # إرجاع رسالة خطأ واضحة
        return False, f"حدث خطأ داخلي أثناء معالجة الاشتراك: {e}", {}


async def _execute_renewal_logic(
        connection: Connection,
        bot: Bot,
        payment_data: dict
) -> tuple[bool, str]:
    """
    مُغلِّف للتجديد الآلي. يستخرج البيانات ويستدعي الدالة الجوهرية.
    """
    # 1. استخراج البيانات من الدفعة
    telegram_id = payment_data.get("telegram_id")
    subscription_plan_id = payment_data.get("subscription_plan_id")
    tx_hash = payment_data.get("tx_hash")
    payment_token = payment_data.get("payment_token")
    amount_received = payment_data.get("amount_received", Decimal('0.0'))

    # 2. جلب تفاصيل الخطة (للحصول على المدة والاسم)
    subscription_plan = await connection.fetchrow(
        "SELECT subscription_type_id, name, duration_days FROM subscription_plans WHERE id = $1",
        subscription_plan_id
    )
    if not subscription_plan:
        return False, f"خطة اشتراك غير صالحة: {subscription_plan_id}"

    subscription_type_id = subscription_plan["subscription_type_id"]
    duration_days = subscription_plan["duration_days"]
    plan_name = subscription_plan["name"]

    # 3. استدعاء الدالة الجوهرية بكل البيانات
    success, message, _ = await _activate_or_renew_subscription_core(
        connection=connection,
        bot=bot,
        telegram_id=telegram_id,
        subscription_type_id=subscription_type_id,
        duration_days=duration_days,
        source="Automatically",
        subscription_plan_id=subscription_plan_id,
        plan_name=plan_name,
        payment_token=payment_token,
        tx_hash=tx_hash,
        amount_received=amount_received
    )

    # لا حاجة للتعامل مع الإشعارات هنا، الدالة الجوهرية قامت بذلك
    return success, message

async def _handle_price_lock_in(
        connection: Connection,
        telegram_id: int,
        plan_id: int,
        subscription_type_id: int,
        amount_received: Decimal
):
    """
    Handles the logic for locking in a discounted price for a user
    if they subscribed during a promotion with the 'lock_in_price' flag.
    """
    try:
        logging.info(f"Checking for price lock-in for user={telegram_id}, plan={plan_id}")

        # الخطوة 1: ابحث عن `user_id`
        user_id_record = await connection.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        if not user_id_record:
            logging.warning(
                f"Price lock-in check failed: User with telegram_id {telegram_id} not found in users table.")
            return

        user_id = user_id_record['id']

        # الخطوة 2: تحقق مما إذا كان لدى المستخدم سعر مثبت بالفعل لهذه الخطة لتجنب العمل غير الضروري
        existing_lock = await connection.fetchval(
            "SELECT 1 FROM user_discounts WHERE user_id = $1 AND subscription_plan_id = $2 AND is_active = true",
            user_id, plan_id
        )
        if existing_lock:
            logging.info(f"User {telegram_id} already has a locked price for plan {plan_id}. Skipping.")
            return

        # الخطوة 3: ابحث عن العرض الذي كان فعالاً عند الاشتراك ويتطلب تثبيت السعر
        # هذا الاستعلام أكثر دقة لأنه يبحث عن العرض العام الذي كان متاحاً
        offer_query = """
            SELECT id as discount_id
            FROM discounts
            WHERE 
                (applicable_to_subscription_type_id = $1 OR applicable_to_subscription_type_id IS NULL)
                AND is_active = true
                AND lock_in_price = true  -- أهم شرط: هل العرض يتطلب تثبيت السعر؟
                AND target_audience = 'all_new'
                AND (start_date IS NULL OR start_date <= NOW())
                AND (end_date IS NULL OR end_date >= NOW())
            ORDER BY 
                -- نعطي الأولوية للعرض المخصص لنوع الاشتراك
                CASE WHEN applicable_to_subscription_type_id IS NOT NULL THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT 1;
        """
        offer = await connection.fetchrow(offer_query, subscription_type_id)

        # الخطوة 4: إذا وجدنا عرضاً مطابقاً، قم بتثبيت السعر
        if offer:
            discount_id = offer['discount_id']
            locked_price = amount_received  # السعر المثبت هو المبلغ الذي دفعه المستخدم بالفعل

            logging.info(
                f"Found active 'lock_in_price' offer (ID: {discount_id}) for user {telegram_id}. Locking price at {locked_price}.")

            # استخدم ON CONFLICT لضمان عدم حدوث خطأ إذا تمت إضافة الخصم في نفس اللحظة من عملية أخرى (أمان إضافي)
            await connection.execute(
                """
                INSERT INTO user_discounts (user_id, discount_id, subscription_plan_id, locked_price, is_active)
                VALUES ($1, $2, $3, $4, true)
                ON CONFLICT (user_id, subscription_plan_id, is_active) DO NOTHING;
                """,
                user_id,
                discount_id,
                plan_id,
                locked_price
            )
            logging.info(
                f"✅ Price locked for user_id={user_id} (telegram_id={telegram_id}) on plan_id={plan_id} at {locked_price}")
        else:
            logging.info(f"No active 'lock_in_price' offer found for user {telegram_id} on plan {plan_id}.")

    except Exception as e:
        # نسجل الخطأ ولكن لا نوقف عملية التجديد الرئيسية بسببه
        logging.error(f"❌ Error during price lock-in check for user {telegram_id}: {e}", exc_info=True)