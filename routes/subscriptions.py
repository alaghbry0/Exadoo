import logging
from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
from database.db_queries import (
    get_user, add_user, add_subscription, update_subscription, add_scheduled_task
)
from utils.db_utils import add_user_to_channel

# إنشاء Blueprint لنقاط API الخاصة بالاشتراكات
subscriptions_bp = Blueprint("subscriptions", __name__)


@subscriptions_bp.route("/api/subscribe", methods=["POST"])
async def subscribe():
    """
    نقطة API للاشتراك أو تجديد الاشتراك.
    """
    try:
        from datetime import datetime, timezone, timedelta

        data = await request.get_json()
        telegram_id = data.get("telegram_id")
        subscription_type_id = data.get("subscription_type_id")
        username = data.get("username", None)
        full_name = data.get("full_name", None)

        logging.info(
            f"📥 Received subscription request: telegram_id={telegram_id}, subscription_type_id={subscription_type_id}")

        if not isinstance(telegram_id, int) or not isinstance(subscription_type_id, int):
            logging.error("❌ Invalid data format: 'telegram_id' and 'subscription_type_id' must be integers.")
            return jsonify(
                {"error": "Invalid data format. 'telegram_id' and 'subscription_type_id' must be integers."}), 400

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
            channel_id = subscription_type["channel_id"]
            duration_days = subscription_type.get("duration_days", 30)  # قيمة افتراضية 30 يوم

            # البحث عن الاشتراك الحالي
            subscription = await connection.fetchrow(
                "SELECT * FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                telegram_id,
                channel_id
            )

            current_time = datetime.now(timezone.utc)  # استخدام التوقيت العالمي


            if subscription:
                # تجديد الاشتراك مع الحفاظ على تاريخ البدء الأصلي
                original_start_date = subscription["start_date"].astimezone(timezone.utc)
                new_expiry = original_start_date + timedelta(days=duration_days)

                # تحديث الاشتراك
                success = await update_subscription(
                    connection,
                    telegram_id,
                    channel_id,
                    subscription_type_id,
                    new_expiry,
                    original_start_date,
                    True
                )



                if not success:
                    logging.error(f"❌ Failed to update subscription for {telegram_id}")
                    return jsonify({"error": "Failed to update subscription"}), 500

                logging.info(f"🔄 Subscription renewed for {telegram_id} until {new_expiry}")
            else:
                # إنشاء اشتراك جديد
                start_date = current_time
                new_expiry = start_date + timedelta(days=duration_days)

                added = await add_subscription(
                    connection,
                    telegram_id,
                    channel_id,
                    subscription_type_id,
                    start_date,
                    new_expiry,
                    True
                )

                if new_expiry is None:
                    logging.error("❌ Critical logic error: new_expiry not set!")
                    return jsonify({"error": "Internal server error"}), 500

                if not added:
                    logging.error(f"❌ Failed to create subscription for {telegram_id}")
                    return jsonify({"error": "Failed to create subscription"}), 500

                logging.info(f"✅ New subscription created for {telegram_id} until {new_expiry}")

            # إضافة المستخدم إلى القناة
            user_added = await add_user_to_channel(telegram_id, channel_id, db_pool)
            if not user_added:
                logging.error(f"❌ Failed to add user {telegram_id} to channel {channel_id}")
                return jsonify({"error": "Failed to add user to channel"}), 500

            # جدولة التذكيرات
            reminders = [
                ("first_reminder", new_expiry - timedelta(hours=24)),
                ("second_reminder", new_expiry - timedelta(hours=1)),
                ("remove_user", new_expiry),
            ]

            for task_type, execute_time in reminders:
                await add_scheduled_task(
                    connection,
                    task_type,
                    telegram_id,
                    channel_id,
                    execute_time.astimezone(timezone.utc)
                )
                logging.info(f"📅 Scheduled '{task_type}' at {execute_time}")

        return jsonify({
            "message": f"✅ تم الاشتراك في {subscription_name} حتى {new_expiry.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "expiry_date": new_expiry.strftime('%Y-%m-%d %H:%M:%S UTC'),
            "start_date": subscription["start_date"].strftime(
                '%Y-%m-%d %H:%M:%S UTC') if subscription else current_time.strftime('%Y-%m-%d %H:%M:%S UTC')
        }), 200

    except Exception as e:
        logging.error(f"❌ Critical error in /api/subscribe: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500