import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import asyncpg
from asyncpg.exceptions import DataError
from datetime import datetime


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)

notifications_bp = Blueprint("notifications", __name__)



# 1. الحصول على قائمة الإشعارات للمستخدم الحالي
@notifications_bp.route("/notifications", methods=["GET"])
async def get_notifications():
    try:
        telegram_id = request.args.get("telegram_id")
        offset = request.args.get("offset", "0")
        limit = request.args.get("limit", "10")

        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    n.id,
                    n.type,
                    n.title,
                    n.message,
                    n.extra_data,
                    n.created_at,
                    un.read_status
                FROM notifications n
                JOIN user_notifications un ON n.id = un.notification_id
                WHERE un.telegram_id = $1
                ORDER BY n.created_at DESC
                OFFSET $2 LIMIT $3;
            """
            results = await connection.fetch(query, int(telegram_id), int(offset), int(limit))

        notifications = [dict(r) for r in results]
        return (
            jsonify(notifications),
            200,
            {
                "Cache-Control": "public, max-age=300",
                "Content-Type": "application/json; charset=utf-8"
            }
        )
    except Exception as e:
        logging.error("Error fetching notifications: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# 2. الحصول على عدد الإشعارات غير المقروءة للمستخدم الحالي
@notifications_bp.route("/notifications/unread/count", methods=["GET"])
async def count_unread_notifications():
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT COUNT(*) AS unread_count
                FROM user_notifications
                WHERE telegram_id = $1 AND read_status = FALSE;
            """
            result = await connection.fetchrow(query, int(telegram_id))
            unread_count = result["unread_count"] if result else 0

        return jsonify({"unread_count": unread_count}), 200

    except Exception as e:
        logging.error("Error counting unread notifications: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# 3. الحصول على عدد الإشعارات غير المقروءة لنوع معين من الإشعارات
@notifications_bp.route("/notifications/unread/count/<notification_type>", methods=["GET"])
async def count_unread_notifications_by_type(notification_type):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT COUNT(*) AS unread_count
                FROM user_notifications un
                JOIN notifications n ON un.notification_id = n.id
                WHERE un.telegram_id = $1 
                  AND un.read_status = FALSE
                  AND n.type = $2;
            """
            result = await connection.fetchrow(query, int(telegram_id), notification_type)
            unread_count = result["unread_count"] if result else 0

        return jsonify({"unread_count": unread_count}), 200
    except Exception as e:
        logging.error("Error counting unread notifications by type: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# 4. تحديد جميع الإشعارات من نوع معين كمقروءة للمستخدم الحالي
@notifications_bp.route("/notifications/mark-as-read/<notification_type>", methods=["PUT"])
async def mark_notifications_as_read(notification_type):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            update_query = """
                UPDATE user_notifications un
                SET read_status = TRUE
                FROM notifications n
                WHERE un.notification_id = n.id 
                  AND un.telegram_id = $1
                  AND n.type = $2;
            """
            await connection.execute(update_query, int(telegram_id), notification_type)

        return jsonify({"message": "Notifications marked as read"}), 200
    except Exception as e:
        logging.error("Error marking notifications as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@notifications_bp.route("/notifications/<int:notification_id>", methods=["GET"])
async def get_notification_details(notification_id):
    """
    نقطة API لجلب تفاصيل إشعار معين للمستخدم، مع بيانات سجل الاشتراك المرتبط (إن وجد).
    يجب تمرير telegram_id كمعلمة query للتحقق من ملكية الإشعار.
    """
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            # استرجاع بيانات الإشعار مع التأكد من أنه ينتمي للمستخدم المطلوب من جدول user_notifications
            query = """
                SELECT 
                    n.id,
                    n.type,
                    n.title,
                    n.message,
                    n.extra_data,
                    n.created_at,
                    un.read_status
                FROM notifications n
                JOIN user_notifications un ON n.id = un.notification_id
                WHERE n.id = $1 AND un.telegram_id = $2
            """
            notification_record = await connection.fetchrow(query, notification_id, int(telegram_id))
            if not notification_record:
                return jsonify({"error": "Notification not found"}), 404

            # تحويل السجل إلى قاموس
            notification_data = dict(notification_record)

            # تحقق مما إذا كانت بيانات extra_data تحتوي على subscription_history_id
            extra_data = notification_data.get("extra_data") or {}
            if isinstance(extra_data, str):
                extra_data = json.loads(extra_data)
            subscription_history_id = extra_data.get("subscription_history_id")

            # إذا وُجد معرف سجل الاشتراك، نجلب بياناته
            if subscription_history_id:
                history_query = """
                    SELECT 
                        subscription_id,
                        invite_link,
                        subscription_type_name,
                        subscription_plan_name,
                        renewal_date,
                        expiry_date,
                        telegram_id,
                        extra_data AS history_extra_data
                    FROM subscription_history
                    WHERE id = $1
                """
                history_record = await connection.fetchrow(history_query, subscription_history_id)
                if history_record:
                    notification_data["subscription_history"] = dict(history_record)
                    # يمكن تحويل التواريخ لتنسيق معين إذا رغبت
                else:
                    notification_data["subscription_history"] = None
            else:
                notification_data["subscription_history"] = None

        return (
            jsonify(notification_data),
            200,
            {
                "Cache-Control": "no-cache",
                "Content-Type": "application/json; charset=utf-8"
            }
        )
    except Exception as e:
        logging.error("Error fetching notification details: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
