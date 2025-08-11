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
from database.tiered_discount_queries import claim_discount_slot_universal, save_user_discount

from typing import Optional
from decimal import Decimal
from utils.db_utils import generate_channel_invite_link, send_message_to_user
from asyncpg import Connection
from aiogram import Bot
from utils.notifications import create_notification
from utils.system_notifications import send_system_notification



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
                break
            else:
                message = renewal_message
                logging.warning(f"⚠️ [Renewal Attempt {attempt} Failed] for user={telegram_id}. Reason: {message}")

        except Exception as e:
            logging.error(f"❌ [Renewal Attempt {attempt} Critical Error] for user={telegram_id}: {e}", exc_info=True)
            message = f"خطأ فادح في نظام التجديد: {e}"

        if not success and attempt < SUBSCRIPTION_RENEWAL_RETRIES:
            logging.info(f"⏳ Retrying in {SUBSCRIPTION_RENEWAL_RETRY_DELAY} seconds...")
            await asyncio.sleep(SUBSCRIPTION_RENEWAL_RETRY_DELAY)

    # --- الخطوة النهائية: تحديث حالة الدفع بناءً على النتيجة ---
    try:
        final_status = "completed" if success else "failed"
        final_error_message = None if success else f"Renewal Failed After Retries: {message}"

        # ===> بداية التعديل: إرسال إشعار عند فشل التجديد النهائي
        if not success:
            await send_system_notification(
                db_pool=current_app.db_pool,
                bot=bot,
                level="ERROR",
                audience="admin",  # الإدارة مسؤولة عن متابعة فشل اشتراكات المستخدمين
                title="فشل تجديد اشتراك مستخدم",
                details={
                    "معرف المستخدم": str(telegram_id),
                    "رمز الدفعة (Token)": payment_token,
                    "السبب": message
                }
            )
        # ===> نهاية التعديل

        await update_payment_with_txhash(
            conn=connection,
            payment_token=payment_token,
            tx_hash=tx_hash,
            amount_received=payment_data['amount_received'],
            status=final_status,
            error_message=final_error_message
        )
        logging.info(f"✅ [Payment Finalized] Payment token={payment_token} status set to '{final_status}'.")

    except Exception as e:
        logging.critical(
            f"CRITICAL ❌ [Payment Finalization Failed] Could not update payment status for token={payment_token}: {e}",
            exc_info=True)

        # ===> بداية التعديل: إرسال إشعار حرج للمطور
        await send_system_notification(
            db_pool=current_app.db_pool,
            bot=bot,
            level="CRITICAL",
            audience="developer",
            title="فشل حرج في تحديث سجل الدفع",
            details={
                "المشكلة": "النظام لم يتمكن من تحديث حالة سجل الدفع بعد اكتمال أو فشل عملية التجديد. هذا قد يسبب عدم تطابق في البيانات.",
                "معرف المستخدم": str(telegram_id),
                "رمز الدفعة (Token)": payment_token,
                "الحالة المفترضة": "completed" if success else "failed",
                "رسالة الخطأ": str(e)
            }
        )
        # ===> نهاية التعديل

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
    # استخدام transaction لضمان أن كل العمليات تنجح معًا أو تفشل معًا
    async with connection.transaction():
        try:
            # --- 1. جلب البيانات الأساسية ---
            user_record = await get_user(connection, telegram_id)
            if not user_record:
                await add_user(connection, telegram_id, username=user_username, full_name=user_full_name)
                logging.info(f"User {telegram_id} not found, added to database.")
                user_record = await get_user(connection, telegram_id)

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

            # --- 5. جدولة مهام القنوات ---
            secondary_links_to_send = []
            for channel in all_channels:
                task_type = "remove_user"
                # جدولة مهمة الإزالة للقناة الرئيسية والفرعية
                await add_scheduled_task(
                    connection=connection, task_type=task_type, telegram_id=telegram_id,
                    execute_at=expiry_date, channel_id=channel['channel_id'], clean_up=True
                )
                logging.info(f"CORE: Scheduled '{task_type}' task for user {telegram_id} from channel {channel['channel_id']} at {expiry_date}.")

                if not channel['is_main']:
                    if channel.get('invite_link'):
                        secondary_links_to_send.append(
                            f"▫️ قناة <a href='{channel['invite_link']}'>{channel['channel_name']}</a>")
                    else:
                        logging.warning(
                            f"CORE: Skipping secondary channel {channel['channel_id']} for user {telegram_id} due to missing invite link.")


            # --- 6. تسجيل في سجل الاشتراكات ---
            previous_history = await connection.fetchval("SELECT 1 FROM subscription_history WHERE payment_id = $1", tx_hash)
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
            action_verb = 'تجديد' if 'RENEWAL' in action_type else 'تفعيل'
            notification_title = f"{action_verb} اشتراك: {subscription_type_name}"
            notification_message = (
                f"🎉 تم بنجاح {action_verb.lower()} اشتراكك في \"{subscription_type_name}\"!\n"
                f"صالح حتى: {expiry_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}."
            )
            notification_extra = {
                "history_id": history_id, "main_invite_link": main_invite_link, "payment_token": payment_token
            }
            await create_notification(
                connection=connection, notification_type="subscription_renewal",
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

            logging.info(f"✅ CORE: Subscription {action_type} for user {telegram_id} processed successfully within transaction.")

            result_data = {
                "new_expiry_date": expiry_date.astimezone(LOCAL_TZ), "greeting_name": greeting_name,
                "subscription_type_name": subscription_type_name, "action_verb": action_verb
            }
            return True, "Subscription processed successfully", result_data

        except Exception as e:
            logging.error(f"❌ CORE: Error in _activate_or_renew_subscription_core for user {telegram_id}: {e}", exc_info=True)
            # إرجاع رسالة خطأ واضحة، والـ transaction سيقوم بـ rollback تلقائياً
            return False, f"حدث خطأ داخلي أثناء معالجة الاشتراك: {e}", {}



# ⭐ تعديل: الدالة أصبحت تستخدم discount_details لتمرير معلومات الخصم
async def _execute_renewal_logic(
        connection: Connection,
        bot: Bot,
        payment_data: dict
) -> tuple[bool, str]:
    """
    يحجز مقعد الخصم، يثبت السعر (إذا لزم الأمر)، ثم يستدعي الدالة الجوهرية.
    """
    telegram_id = payment_data.get("telegram_id")
    subscription_plan_id = payment_data.get("subscription_plan_id")
    amount_received = payment_data.get("amount_received", Decimal('0.0'))
    discount_id_to_claim = payment_data.get("discount_id")
    tier_info_to_save = payment_data.get("tier_info")

    try:
        # هذه الدالة تعمل داخل معاملة (transaction) من الدالة التي تستدعيها
        # الخطوة 1: حجز مقعد في الخصم (إذا كان هناك خصم مطبق)
        if discount_id_to_claim:
            claim_successful, claimed_tier_info = await claim_discount_slot_universal(connection, discount_id_to_claim)
            if not claim_successful:
                return False, "نفدت الكمية المتاحة لهذا العرض أو لم يعد صالحًا."
            if claimed_tier_info: tier_info_to_save = claimed_tier_info

        # الخطوة 2: جلب تفاصيل الخطة
        subscription_plan = await connection.fetchrow("SELECT * FROM subscription_plans WHERE id = $1",
                                                      subscription_plan_id)
        if not subscription_plan: raise ValueError(f"خطة اشتراك غير صالحة: {subscription_plan_id}")

        # ⭐ تعديل: استخراج البيانات اللازمة لتثبيت السعر من الخصم الرئيسي
        discount_details = None
        if discount_id_to_claim:
            discount_details = await connection.fetchrow(
                "SELECT lock_in_price, price_lock_duration_months FROM discounts WHERE id = $1",
                discount_id_to_claim
            )

        # الخطوة 3: معالجة تثبيت السعر
        await _record_discount_usage(  # <-- تم تغيير الاسم هنا
            connection=connection,
            telegram_id=telegram_id,
            plan_id=subscription_plan_id,
            amount_received=amount_received,
            discount_id=discount_id_to_claim,
            tier_info=tier_info_to_save,
            discount_details=discount_details
        )

        # الخطوة 4: استدعاء الدالة الجوهرية لتفعيل أو تجديد الاشتراك
        success, message, _ = await _activate_or_renew_subscription_core(
            connection=connection, bot=bot, telegram_id=telegram_id,
            subscription_type_id=subscription_plan["subscription_type_id"],
            duration_days=subscription_plan["duration_days"], source="Automatically",
            subscription_plan_id=subscription_plan_id, plan_name=subscription_plan["name"],
            payment_token=payment_data.get("payment_token"), tx_hash=payment_data.get("tx_hash"),
            amount_received=amount_received
        )
        if not success: raise Exception(f"Core subscription renewal failed: {message}")

        return success, message
    except Exception as e:
        logging.error(f"Critical error during renewal transaction for user {telegram_id}: {e}", exc_info=True)
        return False, "حدث خطأ فني أثناء محاولة تجديد اشتراكك. تم إلغاء العملية بالكامل."


# ⭐ تعديل: الدالة المحدثة بالكامل للتعامل مع منطق تثبيت السعر
async def _record_discount_usage(
        connection: Connection,
        telegram_id: int,
        plan_id: int,
        amount_received: Decimal,
        discount_id: Optional[int],
        tier_info: Optional[dict],
        discount_details: Optional[dict]
):
    """
    توثق استخدام الخصم في جدول user_discounts.
    - إذا كان الخصم يتطلب تثبيت السعر، تنشئ سجلاً نشطاً.
    - إذا لم يكن يتطلب، تنشئ سجلاً تاريخياً غير نشط.
    """
    # إذا لم يكن هناك خصم، لا تفعل شيئاً
    if not discount_id or not discount_details:
        return

    try:
        user_id_record = await connection.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        if not user_id_record: return
        user_id = user_id_record['id']

        should_lock_price = discount_details.get('lock_in_price', False)

        if should_lock_price:
            # ⭐ الحالة 1: تثبيت السعر مفعل (السلوك القديم)
            logging.info(f"Price lock is enabled for discount {discount_id}. Saving active record for user {user_id}.")
            await save_user_discount(
                conn=connection,
                user_id=user_id,
                plan_id=plan_id,
                discount_id=discount_id,
                locked_price=amount_received,
                tier_info=tier_info,
                lock_duration_months=discount_details.get('price_lock_duration_months'),
                is_active=True  # <-- نمرر True
            )
        else:
            # ⭐ الحالة 2: تثبيت السعر معطل (السلوك الجديد)
            logging.info(f"Price lock is disabled. Saving historical record for discount {discount_id} usage by user {user_id}.")
            await save_user_discount(
                conn=connection,
                user_id=user_id,
                plan_id=plan_id,
                discount_id=discount_id,
                locked_price=amount_received,  # نسجل السعر الذي دفعه
                tier_info=tier_info,
                lock_duration_months=None,  # لا توجد مدة
                is_active=False  # <-- نمرر False
            )

    except Exception as e:
        # من المهم عدم إيقاف عملية التجديد بالكامل بسبب خطأ في التوثيق
        logging.error(f"❌ Non-critical error during discount usage recording for user {telegram_id}: {e}", exc_info=True)
