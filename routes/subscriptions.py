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


# ==============================================================================
# ⚙️ دالة المنطق الفعلي للاشتراك (Worker Function) ⚙️
# ==============================================================================

async def _execute_renewal_logic(
        connection: Connection,
        bot: Bot,
        payment_data: dict
) -> tuple[bool, str]:
    """
    تحتوي هذه الدالة على منطق التجديد الفعلي. لا تتعامل مع إعادة المحاولة أو تحديث حالة الدفع النهائية.
    """
    try:
        # --- استخراج البيانات وتجهيزها ---
        telegram_id = payment_data.get("telegram_id")
        subscription_plan_id = payment_data.get("subscription_plan_id")
        tx_hash = payment_data.get("tx_hash")
        payment_token = payment_data.get("payment_token")

        # تعديل: جلب بيانات المستخدم من قاعدة البيانات بدلاً من استقبالها
        user_record = await get_user(connection, telegram_id)
        if not user_record:
            # هذه الحالة لا يفترض أن تحدث، ولكنها حماية إضافية
            await add_user(connection, telegram_id)
            user_record = await get_user(connection, telegram_id)

        full_name = user_record.get('full_name')
        username = user_record.get('username')

        # --- بداية منطق التجديد الأصلي ---
        subscription_plan = await connection.fetchrow(
            "SELECT id, subscription_type_id, name, duration_days FROM subscription_plans WHERE id = $1",
            subscription_plan_id
        )
        if not subscription_plan:
            return False, f"خطة اشتراك غير صالحة: {subscription_plan_id}"

        subscription_type_info = await connection.fetchrow(
            "SELECT id, name, channel_id AS main_channel_id FROM subscription_types WHERE id = $1",
            subscription_plan["subscription_type_id"]
        )
        if not subscription_type_info or not subscription_type_info["main_channel_id"]:
            return False, f"نوع اشتراك غير مهيأ بقناة رئيسية: {subscription_plan['subscription_type_id']}"

        main_channel_id = int(subscription_type_info["main_channel_id"])
        subscription_type_name = subscription_type_info["name"]

        all_channels_for_type = await connection.fetch(
            "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC, channel_name",
            subscription_plan["subscription_type_id"]
        )
        if not all_channels_for_type:
            return False, f"لا توجد قنوات مرتبطة بنوع الاشتراك: {subscription_plan['subscription_type_id']}"

        # --- حساب التواريخ وإنشاء الروابط (نفس المنطق السابق) ---
        current_time_utc = datetime.now(timezone.utc)
        duration_minutes_dev = 120 if IS_DEVELOPMENT else 0
        calculated_start_date, calculated_new_expiry_date = await calculate_subscription_dates(
            connection, telegram_id, main_channel_id, subscription_plan["duration_days"], duration_minutes_dev,
            current_time_utc
        )

        main_invite_link_generated = None
        main_subscription_record_id = None
        processed_main_channel = False
        secondary_links = []

        for channel in all_channels_for_type:
            channel_id = int(channel["channel_id"])
            channel_name = channel["channel_name"] or f"Channel {channel_id}"
            is_main = channel["is_main"]

            invite_result = await generate_channel_invite_link(bot, telegram_id, channel_id, channel_name)
            if not invite_result["success"]:
                if is_main:
                    return False, f"فشل حرج في إنشاء رابط دعوة للقناة الرئيسية {channel_name}."
                logging.warning(f"Skipping secondary channel {channel_name} due to invite link failure.")
                continue

            invite_link = invite_result["invite_link"]

            if is_main:
                existing_sub = await connection.fetchrow(
                    "SELECT id FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2", telegram_id, channel_id)
                if existing_sub:
                    await update_subscription(connection, telegram_id, channel_id,
                                              subscription_plan["subscription_type_id"], calculated_new_expiry_date,
                                              calculated_start_date, True, subscription_plan_id, tx_hash, invite_link,
                                              "Automatically")
                    main_subscription_record_id = existing_sub['id']
                else:
                    main_subscription_record_id = await add_subscription(connection, telegram_id, channel_id,
                                                                         subscription_plan["subscription_type_id"],
                                                                         calculated_start_date,
                                                                         calculated_new_expiry_date, True,
                                                                         subscription_plan_id, tx_hash, invite_link,
                                                                         "Automatically", returning_id=True)

                if not main_subscription_record_id:
                    return False, "فشل في إنشاء أو تحديث سجل الاشتراك الرئيسي."

                main_invite_link_generated = invite_link
                processed_main_channel = True
            else:
                secondary_links.append(f"▫️ قناة <a href='{invite_link}'>{channel_name}</a>")
                await connection.execute(
                    "DELETE FROM scheduled_tasks WHERE task_type = 'remove_user' AND telegram_id = $1 AND channel_id = $2",
                    telegram_id, channel_id)
                await add_scheduled_task(connection, "remove_user", telegram_id, channel_id, calculated_new_expiry_date)

        if not processed_main_channel or not main_invite_link_generated:
            return False, "لم تتم معالجة القناة الرئيسية أو إنشاء رابط لها بنجاح."

        # --- إرسال الرسائل وتحديث السجلات (نفس المنطق السابق) ---
        if secondary_links:
            msg_text = (f"📬 مرحبًا {full_name or username or telegram_id},\n\n"
                        f"اشتراكك في \"{subscription_type_name}\" مفعل الآن!\n"
                        "بالإضافة إلى القناة الرئيسية، يمكنك الانضمام إلى القنوات الفرعية التالية:\n\n" +
                        "\n".join(secondary_links) +
                        "\n\n💡 هذه الروابط خاصة بك وصالحة لفترة محدودة. يرجى الانضمام في أقرب وقت.")
            await send_message_to_user(bot, telegram_id, msg_text)

        previous_history = await connection.fetchval("SELECT 1 FROM subscription_history WHERE payment_id = $1",
                                                     tx_hash)
        action_type = 'RENEWAL' if previous_history else 'NEW'

        history_data = json.dumps({"full_name": full_name, "username": username, "payment_id_ref": tx_hash})
        history_record = await connection.fetchrow(
            "INSERT INTO subscription_history (subscription_id, invite_link, action_type, subscription_type_name, subscription_plan_name, renewal_date, expiry_date, telegram_id, extra_data, payment_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            main_subscription_record_id, main_invite_link_generated, action_type, subscription_type_name,
            subscription_plan["name"], calculated_start_date, calculated_new_expiry_date, telegram_id, history_data,
            tx_hash
        )

        # --- إنشاء الإشعار النهائي للنجاح ---
        notification_title = f"{'تجديد' if action_type == 'RENEWAL' else 'تفعيل'} اشتراك: {subscription_type_name}"
        notification_message = (
            f"🎉 تم بنجاح {'تجديد' if action_type == 'RENEWAL' else 'تفعيل'} اشتراكك في \"{subscription_type_name}\"!\n"
            f"صالح حتى: {calculated_new_expiry_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}.")
        notification_extra = {"history_id": history_record["id"] if history_record else None,
                              "main_invite_link": main_invite_link_generated, "payment_token": payment_token}

        await create_notification(connection=connection, notification_type="subscription_update",
                                  title=notification_title, message=notification_message, extra_data=notification_extra,
                                  is_public=False, telegram_ids=[telegram_id])

        logging.info(f"✅ Logic executed successfully for user={telegram_id}")
        return True, notification_message

    except Exception as e:
        # التقاط أي خطأ أثناء التنفيذ وإعادته كرسالة فشل
        logging.error(f"❌ Error in _execute_renewal_logic for user={payment_data.get('telegram_id')}: {e}",
                      exc_info=True)
        return False, f"حدث خطأ داخلي أثناء تفعيل الاشتراك: {e}"