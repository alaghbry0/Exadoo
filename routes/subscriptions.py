import logging
import pytz
from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta, timezone  # <-- إضافة timezone
from database.db_queries import (
    get_user, add_user, add_subscription, update_subscription, add_scheduled_task
)
from utils.db_utils import add_user_to_channel

# إنشاء Blueprint لنقاط API الخاصة بالاشتراكات
subscriptions_bp = Blueprint("subscriptions", __name__)

LOCAL_TZ = pytz.timezone("Asia/Riyadh")  # يمكنك تغييره حسب منطقتك الزمنية

# تحديد إذا كان التطبيق في وضع الاختبار
IS_DEVELOPMENT = True  # يمكن تغيير هذا بناءً على إعدادات البيئة (مثال: os.getenv("ENVIRONMENT") == "development")


@subscriptions_bp.route("/api/subscribe", methods=["POST"])
async def subscribe():
    """
    نقطة API للاشتراك أو تجديد الاشتراك.
    """
    try:
        data = await request.get_json()
        telegram_id = data.get("telegram_id")
        subscription_type_id = data.get("subscription_type_id")
        username = data.get("username", None)
        full_name = data.get("full_name", None)

        logging.info(f"📥 Received subscription request: telegram_id={telegram_id}, subscription_type_id={subscription_type_id}")

        if not isinstance(telegram_id, int) or not isinstance(subscription_type_id, int):
            logging.error("❌ Invalid data format: 'telegram_id' and 'subscription_type_id' must be integers.")
            return jsonify({"error": "Invalid data format. 'telegram_id' and 'subscription_type_id' must be integers."}), 400

        db_pool = current_app.db_pool if hasattr(current_app, "db_pool") else None
        if not db_pool:
            logging.error("❌ Database connection is missing!")
            return jsonify({"error": "Internal Server Error"}), 500

        async with db_pool.acquire() as connection:
            # إدارة بيانات المستخدم
            user = await get_user(connection, telegram_id)
            if not user:
                added = await add_user(connection, telegram_id, username=username, full_name=full_name)
                if not added:
                    logging.error(f"❌ Failed to add user {telegram_id}")
                    return jsonify({"error": "Failed to register user"}), 500

            # جلب تفاصيل نوع الاشتراك مع المدة
            subscription_type = await connection.fetchrow(
                "SELECT id, name, channel_id, duration_days FROM subscription_types WHERE id = $1",
                subscription_type_id
            )
            if not subscription_type:
                logging.error(f"❌ Invalid subscription_type_id: {subscription_type_id}")
                return jsonify({"error": "Invalid subscription type."}), 400

            subscription_name = subscription_type["name"]
            channel_id = int(subscription_type["channel_id"])

            # استخدام مدة قصيرة في وضع الاختبار
            if IS_DEVELOPMENT:
                duration_days = 0
                duration_minutes = 5
            else:
                duration_days = subscription_type.get("duration_days", 30)
                duration_minutes = 0

            # الحصول على التوقيت الحالي بالتوقيت العالمي (UTC)
            current_time = datetime.now(timezone.utc)

            # البحث عن الاشتراك الحالي
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
                    subscription_type_id,
                    new_expiry,
                    start_date,
                    True
                )
                if not success:
                    logging.error(f"❌ Failed to update subscription for {telegram_id}")
                    return jsonify({"error": "Failed to update subscription"}), 500

                logging.info(f"🔄 Subscription renewed for {telegram_id} until {new_expiry}")

            else:
                start_date = current_time
                new_expiry = start_date + timedelta(days=duration_days, minutes=duration_minutes)

                added = await add_subscription(
                    connection,
                    telegram_id,
                    channel_id,
                    subscription_type_id,
                    start_date,
                    new_expiry,
                    True
                )
                if not added:
                    logging.error(f"❌ Failed to create subscription for {telegram_id}")
                    return jsonify({"error": "Failed to create subscription"}), 500

                logging.info(f"✅ New subscription created for {telegram_id} until {new_expiry}")

            # إضافة المستخدم إلى القناة
            user_added = await add_user_to_channel(telegram_id, subscription_type_id, db_pool)
            if not user_added:
                logging.error(f"❌ Failed to add user {telegram_id} to channel {channel_id}")
                return jsonify({"error": "Failed to add user to channel"}), 500

            # جدولة التذكيرات
            if IS_DEVELOPMENT:
                reminders = [
                    ("first_reminder", new_expiry - timedelta(minutes=2)),
                    ("second_reminder", new_expiry - timedelta(minutes=1)),
                    ("remove_user", new_expiry),
                ]
            else:
                reminders = [
                    ("first_reminder", new_expiry - timedelta(hours=24)),
                    ("second_reminder", new_expiry - timedelta(hours=1)),
                    ("remove_user", new_expiry),
                ]

            for task_type, execute_time in reminders:
                if execute_time.tzinfo is None:
                    execute_time = execute_time.replace(tzinfo=timezone.utc)

                # تحويل التوقيت إلى UTC+3 قبل تخزينه أو تسجيله في السجلات
                execute_time_local = execute_time.astimezone(LOCAL_TZ)

                await add_scheduled_task(
                    connection,
                    task_type,
                    telegram_id,
                    channel_id,
                    execute_time
                )
                logging.info(f"📅 Scheduled '{task_type}' at {execute_time_local}")

            # تحويل `start_date` و `new_expiry` إلى التوقيت المحلي (UTC+3)
            start_date_local = start_date.astimezone(LOCAL_TZ)
            new_expiry_local = new_expiry.astimezone(LOCAL_TZ)

            return jsonify({
                "message": f"✅ تم الاشتراك في {subscription_name} حتى {new_expiry_local.strftime('%Y-%m-%d %H:%M:%S UTC+3')}",
                "expiry_date": new_expiry_local.strftime('%Y-%m-%d %H:%M:%S UTC+3'),
                "start_date": start_date_local.strftime('%Y-%m-%d %H:%M:%S UTC+3')
            }), 200

    except Exception as e:
        logging.error(f"❌ Critical error in /api/subscribe: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500