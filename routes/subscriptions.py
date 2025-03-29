import logging
import pytz
import os
from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta, timezone
from database.db_queries import (
    get_user, add_user, add_subscription, update_subscription, add_scheduled_task
)
from utils.db_utils import add_user_to_channel
import json
from server.redis_manager import redis_manager


# إنشاء Blueprint لنقاط API الخاصة بالاشتراكات
subscriptions_bp = Blueprint("subscriptions", __name__)

LOCAL_TZ = pytz.timezone("Asia/Riyadh")  # التوقيت المحلي
IS_DEVELOPMENT = True  # يمكن تغييره بناءً على بيئة التطبيق

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # تحميل مفتاح Webhook من المتغيرات البيئية


@subscriptions_bp.route("/api/subscribe", methods=["POST"])
async def subscribe():
    """
    نقطة API لتجديد الاشتراك أو إنشاء اشتراك جديد بعد تأكيد الدفع.
    تُعيد الرد بيانات الاشتراك ورابط الدعوة.
    """
    try:
        import json
        secret = request.headers.get("Authorization")
        if not secret or secret != f"Bearer {WEBHOOK_SECRET}":
            logging.warning("❌ Unauthorized request: Invalid or missing WEBHOOK_SECRET")
            return jsonify({"error": "Unauthorized request"}), 403

        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/subscribe: {json.dumps(data, indent=2)}")

        telegram_id = data.get("telegram_id")
        subscription_plan_id = data.get("subscription_plan_id")
        payment_id = data.get("payment_id")
        payment_token = data.get("payment_token")  # قراءة payment_token من البيانات الواردة
        username = data.get("username", None)
        full_name = data.get("full_name", None)

        logging.info(f"📥 Received subscription request: telegram_id={telegram_id}, "
                     f"subscription_plan_id={subscription_plan_id}, payment_id={payment_id}")

        if not isinstance(telegram_id, int) or not isinstance(subscription_plan_id, int) or not isinstance(payment_id, str):
            logging.error(f"❌ Invalid data format: telegram_id={telegram_id}, "
                          f"subscription_plan_id={subscription_plan_id}, payment_id={payment_id}")
            return jsonify({"error": "Invalid data format"}), 400

        # التأكد من وجود payment_token
        if not payment_token:
            logging.error("❌ Missing payment_token in the request data")
            return jsonify({"error": "Missing payment_token"}), 400

        logging.info(f"✅ استلام طلب اشتراك: telegram_id={telegram_id}, "
                     f"subscription_plan_id={subscription_plan_id}, payment_id={payment_id}")

        db_pool = getattr(current_app, "db_pool", None)
        if not db_pool:
            logging.critical("❌ Database connection is missing!")
            return jsonify({"error": "Internal Server Error"}), 500

        async with db_pool.acquire() as connection:
            existing_payment = await connection.fetchrow(
                "SELECT * FROM payments WHERE tx_hash = $1", payment_id
            )
            if existing_payment:
                logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                existing_subscription = await connection.fetchrow(
                    "SELECT * FROM subscriptions WHERE telegram_id = $1 AND subscription_type_id = $2 AND payment_id = $3",
                    telegram_id, existing_payment.get("subscription_type_id"), payment_id
                )
                if existing_subscription:
                    logging.info(f"✅ الاشتراك محدث بالفعل لـ {telegram_id}, لا حاجة للتحديث.")
                    return jsonify({"message": "Subscription already updated"}), 200

        async with db_pool.acquire() as connection:
            user = await get_user(connection, telegram_id)
            if not user:
                added = await add_user(connection, telegram_id, username=username, full_name=full_name)
                if not added:
                    logging.error(f"❌ Failed to add user {telegram_id}")
                    return jsonify({"error": "Failed to register user"}), 500

            subscription_plan = await connection.fetchrow(
                "SELECT id, subscription_type_id, name, duration_days FROM subscription_plans WHERE id = $1",
                subscription_plan_id
            )
            if not subscription_plan:
                logging.error(f"❌ Invalid subscription_plan_id: {subscription_plan_id}")
                return jsonify({"error": "Invalid subscription plan."}), 400

            duration_days = subscription_plan["duration_days"]

            subscription_type = await connection.fetchrow(
                "SELECT id, name, channel_id FROM subscription_types WHERE id = $1",
                subscription_plan["subscription_type_id"]
            )
            if not subscription_type:
                logging.error(f"❌ Invalid subscription type for plan {subscription_plan_id}")
                return jsonify({"error": "Invalid subscription type."}), 400

            subscription_name = subscription_type["name"]
            channel_id = int(subscription_type["channel_id"])

            duration_minutes = 120 if IS_DEVELOPMENT else 0
            current_time = datetime.now(timezone.utc)

            subscription = await connection.fetchrow(
                "SELECT * FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                telegram_id,
                channel_id
            )

            if subscription:
                is_subscription_active = subscription['is_active'] and subscription['expiry_date'] >= current_time
                if is_subscription_active:
                    new_expiry = subscription['expiry_date'] + timedelta(minutes=duration_minutes, days=duration_days)
                    start_date = subscription['start_date']
                else:
                    start_date = current_time
                    new_expiry = start_date + timedelta(minutes=duration_minutes, days=duration_days)

                success = await update_subscription(
                    connection,
                    telegram_id,
                    channel_id,
                    subscription_plan["subscription_type_id"],
                    new_expiry,
                    start_date,
                    True,
                    payment_id
                )
                if not success:
                    logging.error(f"❌ Failed to update subscription for {telegram_id}")
                    return jsonify({"error": "Failed to update subscription"}), 500

                logging.info(f"🔄 Subscription renewed for {telegram_id} until {new_expiry}")

            else:
                start_date = current_time
                new_expiry = start_date + timedelta(minutes=duration_minutes, days=duration_days)

                added = await add_subscription(
                    connection,
                    telegram_id,
                    channel_id,
                    subscription_plan["subscription_type_id"],
                    start_date,
                    new_expiry,
                    True,
                    payment_id
                )
                if not added:
                    logging.error(f"❌ Failed to create subscription for {telegram_id}")
                    return jsonify({"error": "Failed to create subscription"}), 500

                logging.info(f"✅ New subscription created for {telegram_id} until {new_expiry}")

            # استدعاء دالة add_user_to_channel للحصول على رابط الدعوة (دون تخزينه في قاعدة البيانات)
            channel_result = await add_user_to_channel(telegram_id, subscription_plan["subscription_type_id"], db_pool)
            invite_link = channel_result.get("invite_link")

            # جدولة التذكيرات (مثال، يمكن تعديله حسب الحاجة)
            reminders = [
                ("first_reminder", new_expiry - timedelta(minutes=30 if IS_DEVELOPMENT else 1440)),
                ("second_reminder", new_expiry - timedelta(minutes=15 if IS_DEVELOPMENT else 60)),
                ("remove_user", new_expiry),
            ]
            for task_type, execute_time in reminders:
                if execute_time.tzinfo is None:
                    execute_time = execute_time.replace(tzinfo=timezone.utc)
                execute_time_local = execute_time.astimezone(LOCAL_TZ)
                await add_scheduled_task(
                    connection,
                    task_type,
                    telegram_id,
                    channel_id,
                    execute_time
                )
                logging.info(f"📅 Scheduled '{task_type}' at {execute_time_local}")

            start_date_local = start_date.astimezone(LOCAL_TZ)
            new_expiry_local = new_expiry.astimezone(LOCAL_TZ)

            response_data = {
                "fmessage": f"✅ تم الاشتراك في {subscription_name} حتى {new_expiry_local.strftime('%Y-%m-%d %H:%M:%S UTC+3')}",
                "expiry_date": new_expiry_local.strftime('%Y-%m-%d %H:%M:%S UTC+3'),
                "start_date": start_date_local.strftime('%Y-%m-%d %H:%M:%S UTC+3'),
                "invite_link": invite_link,
                "formatted_message": f"تم تفعيل اشتراكك بنجاح! اضغط <a href='{invite_link}' target='_blank'>هنا</a> للانضمام إلى القناة."
            }

            # نشر الحدث عبر Redis باستخدام payment_token
            await redis_manager.publish_event(
                f"payment_{payment_token}",
                {
                    'status': 'success',
                    'type': 'subscription_success',
                    'message': f'✅ تم الاشتراك في {subscription_name} حتى {new_expiry_local.strftime("%Y-%m-%d %H:%M:%S UTC+3")}',
                    'invite_link': invite_link,
                    'formatted_message': f"تم تفعيل اشتراكك بنجاح! اضغط <a href='{invite_link}' target='_blank'>هنا</a> للانضمام إلى القناة."

                }
            )

            return jsonify(response_data), 200

    except Exception as e:
        logging.error(f"❌ Critical error in /api/subscribe: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500



