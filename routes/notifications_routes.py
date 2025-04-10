import json
import logging
from quart import Blueprint, request, jsonify, current_app
from database.db_queries import get_unread_notifications_count
from config import DATABASE_CONFIG
import asyncpg
from asyncpg.exceptions import DataError
from datetime import datetime
from utils.notifications import create_notification
from routes.ws_routes import broadcast_new_notification, broadcast_unread_count

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
        filter_type = request.args.get("filter", "all")  # إضافة معلمة لتصفية الإشعارات

        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            # تعديل الاستعلام للدعم التصفية حسب حالة القراءة
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
            """
            
            params = [int(telegram_id)]
            
            # إضافة شرط للإشعارات غير المقروءة فقط
            if filter_type == "unread":
                query += " AND un.read_status = FALSE"
                
            query += """
                ORDER BY n.created_at DESC
                OFFSET $2 LIMIT $3;
            """
            
            params.extend([int(offset), int(limit)])
            
            results = await connection.fetch(query, *params)

        notifications = [dict(r) for r in results]
        
        # إضافة بيانات التصنيف للإشعارات لتحسين عرض الواجهة
        for notification in notifications:
            if isinstance(notification.get("extra_data"), str) and notification["extra_data"]:
                try:
                    notification["extra_data"] = json.loads(notification["extra_data"])
                except json.JSONDecodeError:
                    notification["extra_data"] = {}
            
            # إضافة معلومات تصنيف لتسهيل التحكم في العرض
            notification["category"] = categorize_notification(notification["type"])
        
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


# وظيفة مساعدة لتصنيف الإشعارات
def categorize_notification(notification_type):
    if notification_type.startswith("subscription_"):
        return "subscription"
    elif notification_type.startswith("payment_"):
        return "payment"
    elif notification_type.startswith("system_"):
        return "system"
    else:
        return "other"


# 2. الحصول على عدد الإشعارات غير المقروءة للمستخدم الحالي
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
            
            # إحضار عدد الإشعارات غير المقروءة المحدث وإرساله للعميل
            unread_count = await get_unread_notifications_count(connection, int(telegram_id))
            
            # بث التحديث عبر WebSocket
            await broadcast_unread_count(telegram_id, unread_count)

        return jsonify({"message": "Notifications marked as read", "unread_count": unread_count}), 200
    except Exception as e:
        logging.error("Error marking notifications as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# إضافة مسار جديد لتحديد إشعار محدد كمقروء
@notifications_bp.route("/notifications/<int:notification_id>/mark-read", methods=["PUT"])
async def mark_single_notification_as_read(notification_id):
    try:
        telegram_id = request.args.get("telegram_id")
        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            # التحقق من وجود الإشعار للمستخدم المحدد
            check_query = """
                SELECT 1 FROM user_notifications 
                WHERE notification_id = $1 AND telegram_id = $2
            """
            exists = await connection.fetchval(check_query, notification_id, int(telegram_id))
            
            if not exists:
                return jsonify({"error": "Notification not found or not authorized"}), 404
            
            # تحديث حالة القراءة
            update_query = """
                UPDATE user_notifications
                SET read_status = TRUE
                WHERE notification_id = $1 AND telegram_id = $2 AND read_status = FALSE
                RETURNING 1
            """
            updated = await connection.fetchval(update_query, notification_id, int(telegram_id))
            
            # إحضار عدد الإشعارات غير المقروءة المحدث
            unread_count = await get_unread_notifications_count(connection, int(telegram_id))
            
            # بث التحديث عبر WebSocket
            await broadcast_unread_count(telegram_id, unread_count)
            
            result = {
                "success": updated is not None,
                "already_read": updated is None,
                "unread_count": unread_count
            }
            
        return jsonify(result), 200
    except Exception as e:
        logging.error("Error marking notification as read: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@notifications_bp.route("/notification/<int:notification_id>", methods=["GET"])
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
                
            # إذا كان الإشعار غير مقروء، نقوم بتحديث حالة القراءة
            if not notification_data["read_status"]:
                update_query = """
                    UPDATE user_notifications
                    SET read_status = TRUE
                    WHERE notification_id = $1 AND telegram_id = $2
                    RETURNING 1
                """
                updated = await connection.fetchval(update_query, notification_id, int(telegram_id))
                
                if updated:
                    notification_data["read_status"] = True
                    
                    # إحضار عدد الإشعارات غير المقروءة المحدث
                    unread_count = await get_unread_notifications_count(connection, int(telegram_id))
                    
                    # بث التحديث عبر WebSocket
                    await broadcast_unread_count(telegram_id, unread_count)

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