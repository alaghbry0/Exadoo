import asyncio
import json
import logging
from quart import Blueprint, request, jsonify, current_app, Response
from database.db_queries import get_unread_notifications_count


# ==============================================================================
# تهيئة الـ Blueprint والوظائف المساعدة
# ==============================================================================

notifications_bp = Blueprint("notifications", __name__)


def categorize_notification(notification_type):
    """وظيفة مساعدة لتصنيف الإشعارات بناءً على نوعها."""
    if notification_type.startswith("subscription_"):
        return "subscription"
    elif notification_type.startswith("payment_"):
        return "payment"
    elif notification_type.startswith("system_"):
        return "system"
    else:
        return "other"

# ==============================================================================
# نقاط النهاية (Endpoints) لـ REST API (مدمجة ومحدثة)
# ==============================================================================

# 1. الحصول على قائمة الإشعارات (مع الحفاظ على منطق التصنيف)
# 1. الحصول على قائمة الإشعارات (مع الحفاظ على منطق التصنيف)
@notifications_bp.route("/notifications", methods=["GET"])
async def get_notifications():
    try:
        telegram_id = request.args.get("telegram_id")
        offset = request.args.get("offset", "0")
        limit = request.args.get("limit", "10")
        filter_type = request.args.get("filter", "all")

        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT n.id, n.type, n.title, n.message, n.extra_data, n.created_at, un.read_status
                FROM notifications n JOIN user_notifications un ON n.id = un.notification_id
                WHERE un.telegram_id = $1
            """
            params = [int(telegram_id)]
            if filter_type == "unread":
                query += " AND un.read_status = FALSE"
            query += " ORDER BY n.created_at DESC OFFSET $2 LIMIT $3;"
            params.extend([int(offset), int(limit)])
            results = await connection.fetch(query, *params)

        notifications = [dict(r) for r in results]

        for notification in notifications:
            if isinstance(notification.get("extra_data"), str) and notification["extra_data"]:
                try:
                    notification["extra_data"] = json.loads(notification["extra_data"])
                except json.JSONDecodeError:
                    notification["extra_data"] = {}
            notification["category"] = categorize_notification(notification["type"])

        return jsonify(notifications), 200
    except Exception as e:
        logging.error("Error fetching notifications: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# 2. الحصول على تفاصيل إشعار معين (مع تحديث الحالة والبث عبر SSE)
@notifications_bp.route("/notification/<int:notification_id>", methods=["GET"])
async def get_notification_details(notification_id):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT n.id, n.type, n.title, n.message, n.extra_data, n.created_at, un.read_status
                FROM notifications n JOIN user_notifications un ON n.id = un.notification_id
                WHERE n.id = $1 AND un.telegram_id = $2
            """
            notification_record = await connection.fetchrow(query, notification_id, int(telegram_id))
            if not notification_record:
                return jsonify({"error": "Notification not found"}), 404

            notification_data = dict(notification_record)
            extra_data = notification_data.get("extra_data") or {}
            if isinstance(extra_data, str):
                extra_data = json.loads(extra_data)
            subscription_history_id = extra_data.get("subscription_history_id")

            if subscription_history_id:
                history_query = "SELECT * FROM subscription_history WHERE id = $1"
                history_record = await connection.fetchrow(history_query, subscription_history_id)
                notification_data["subscription_history"] = dict(history_record) if history_record else None
            
            if not notification_data["read_status"]:
                update_query = "UPDATE user_notifications SET read_status = TRUE WHERE notification_id = $1 AND telegram_id = $2 RETURNING 1"
                updated = await connection.fetchval(update_query, notification_id, int(telegram_id))
                
                if updated:
                    notification_data["read_status"] = True
                    unread_count = await get_unread_notifications_count(connection, int(telegram_id))
                    # ✨ بث تحديث العدد عبر SSE
                    await current_app.sse_client.publish(telegram_id, 'unread_update', {"count": unread_count})

        return jsonify(notification_data), 200
    except Exception as e:
        logging.error("Error fetching notification details: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# 3. الحصول على عدد الإشعارات غير المقروءة
@notifications_bp.route("/notifications/unread-count", methods=["GET"])
async def count_unread_notifications():
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400
        async with current_app.db_pool.acquire() as connection:
            unread_count = await get_unread_notifications_count(connection, int(telegram_id))
        return jsonify({"unread_count": unread_count}), 200
    except Exception as e:
        logging.error("Error counting unread notifications: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# 4. تحديد إشعار واحد كمقروء (محدث لاستخدام SSE)
@notifications_bp.route("/notifications/<int:notification_id>/mark-read", methods=["PUT"])
async def mark_single_notification_as_read(notification_id):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            update_query = """
                UPDATE user_notifications
                SET read_status = TRUE
                WHERE notification_id = $1 AND telegram_id = $2 AND read_status = FALSE
                RETURNING 1
            """
            updated = await connection.fetchval(update_query, notification_id, int(telegram_id))
            
            if updated:
                unread_count = await get_unread_notifications_count(connection, int(telegram_id))
                # ✨ بث تحديث العدد عبر SSE
                await current_app.sse_client.publish(
                    telegram_id, 'unread_update', {"count": unread_count}
                )
            
        return jsonify({"success": updated is not None}), 200
    except Exception as e:
        logging.error("Error marking notification as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# 5. تحديد كل الإشعارات كمقروءة (محدث لاستخدام SSE)
@notifications_bp.route("/notifications/mark-all-read", methods=["PUT"])
async def mark_all_as_read():
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            update_query = "UPDATE user_notifications SET read_status = TRUE WHERE telegram_id = $1 AND read_status = FALSE"
            await connection.execute(update_query, int(telegram_id))

            # ✨ بث تحديث العدد عبر SSE (العدد الآن صفر)
            await current_app.sse_client.publish(telegram_id, 'unread_update', {"count": 0})

        return jsonify({"message": "All notifications marked as read"}), 200
    except Exception as e:
        logging.error("Error marking all notifications as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
        
# 6. تحديد جميع الإشعارات من نوع معين كمقروءة (تم الحفاظ عليه وتحديثه لـ SSE)
@notifications_bp.route("/notifications/mark-as-read/<notification_type>", methods=["PUT"])
async def mark_notifications_as_read_by_type(notification_type):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            update_query = """
                UPDATE user_notifications un SET read_status = TRUE FROM notifications n
                WHERE un.notification_id = n.id AND un.telegram_id = $1 AND n.type = $2;
            """
            await connection.execute(update_query, int(telegram_id), notification_type)
            
            unread_count = await get_unread_notifications_count(connection, int(telegram_id))
            
            # ✨ بث تحديث العدد عبر SSE
            await current_app.sse_client.publish(telegram_id, 'unread_update', {"count": unread_count}
            )

        return jsonify({"message": f"Notifications of type {notification_type} marked as read", "unread_count": unread_count}), 200
    except Exception as e:
        logging.error("Error marking notifications by type as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    
