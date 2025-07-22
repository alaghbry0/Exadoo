# utils/notifications.py (النسخة النهائية المحدثة مع SSE)

import json
import logging
from datetime import datetime, timezone
from quart import current_app
from database.db_queries import get_unread_notifications_count


async def create_notification(
        connection,
        notification_type: str,
        title: str,
        message: str,
        extra_data: dict = None,
        is_public: bool = False,
        telegram_ids: list = None
):
    """
    تنشئ إشعارًا جديدًا وتبثه فورًا للمستخدمين المعنيين عبر Server-Sent Events (SSE).

    :param connection: اتصال قاعدة البيانات.
    :param notification_type: نوع الإشعار.
    :param title: عنوان الإشعار.
    :param message: نص الرسالة.
    :param extra_data: بيانات إضافية (JSON).
    :param is_public: إذا كان الإشعار عامًا.
    :param telegram_ids: قائمة بمعرفات تليجرام المستهدفة.
    :return: معرف الإشعار المُنشأ.
    """
    extra_data = extra_data or {}
    notification_query = """
        INSERT INTO notifications (type, title, message, extra_data, is_public)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        RETURNING id
    """
    record = await connection.fetchrow(
        notification_query,
        notification_type,
        title,
        message,
        json.dumps(extra_data),
        is_public
    )
    notification_id = record["id"]

    # إنشاء كائن الإشعار للبث
    notification_obj = {
        "id": notification_id,
        "type": notification_type,
        "title": title,
        "message": message,
        "extra_data": extra_data,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read_status": False
    }

    target_user_ids = []
    if not is_public:
        if not telegram_ids:
            raise ValueError("telegram_ids is required for private notifications")
        target_user_ids = [telegram_ids] if not isinstance(telegram_ids, list) else telegram_ids
        
        insert_query = "INSERT INTO user_notifications (telegram_id, notification_id) VALUES ($1, $2)"
        for tg_id in target_user_ids:
            await connection.execute(insert_query, tg_id, notification_id)
    else:
        # للإشعارات العامة، أدرجها لجميع المستخدمين
        insert_query = "INSERT INTO user_notifications (telegram_id, notification_id) SELECT telegram_id, $1 FROM users"
        await connection.execute(insert_query, notification_id)
        
        # واحصل على قائمة بجميع المستخدمين لإرسال الإشعار لهم
        users = await connection.fetch("SELECT telegram_id FROM users")
        target_user_ids = [user['telegram_id'] for user in users]

    # ✨ التغيير هنا: البث باستخدام SSE
    broadcaster = current_app.sse_client
    for tg_id in target_user_ids:
        try:
            # 1. بث الإشعار الجديد نفسه
            await broadcaster.publish(tg_id, 'new_notification', notification_obj)
            
            # 2. جلب العدد المحدث وبثه
            unread_count = await get_unread_notifications_count(connection, tg_id)
            await broadcaster.publish(tg_id, 'unread_update', {"count": unread_count})

        except Exception as e:
            logging.error(f"Error publishing SSE event to {tg_id}: {e}")

    return notification_id