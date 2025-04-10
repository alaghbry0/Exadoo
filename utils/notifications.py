import json
import logging
from datetime import datetime
from routes.ws_routes import broadcast_new_notification

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
    تنشئ إشعارًا جديدًا في جدول notifications ثم تربطه بالمستخدمين في user_notifications.

    :param connection: اتصال قاعدة البيانات.
    :param notification_type: نوع الإشعار (مثال: "subscription_renewal" أو "payment_success").
    :param title: عنوان مختصر للإشعار.
    :param message: نص الرسالة الأساسية للإشعار.
    :param extra_data: بيانات إضافية.
    :param is_public: إذا كان الإشعار عامًا لجميع المستخدمين.
    :param telegram_ids: قائمة بمعرفات تليجرام للمستخدمين المستهدفين (للإشعارات الخاصة).
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

    # إنشاء كائن الإشعار للإرسال عبر WebSocket
    notification_obj = {
        "id": notification_id,
        "type": notification_type,
        "title": title,
        "message": message,
        "extra_data": extra_data,
        "created_at": datetime.now().isoformat(),
        "read_status": False
    }

    if not is_public:
        if not telegram_ids:
            raise ValueError("telegram_ids is required for private notifications")
        if not isinstance(telegram_ids, list):
            telegram_ids = [telegram_ids]
        insert_query = """
            INSERT INTO user_notifications (telegram_id, notification_id)
            VALUES ($1, $2)
        """
        for tg_id in telegram_ids:
            await connection.execute(insert_query, tg_id, notification_id)
            
            # إرسال الإشعار الجديد عبر WebSocket للمستخدم المعني
            try:
                await broadcast_new_notification(tg_id, notification_obj)
            except Exception as e:
                logging.error(f"Error broadcasting notification to {tg_id}: {e}")
    else:
        # للإشعارات العامة، نقوم بإدراجها لجميع المستخدمين
        insert_query = """
            INSERT INTO user_notifications (telegram_id, notification_id)
            SELECT telegram_id, $1 FROM users
        """
        await connection.execute(insert_query, notification_id)
        
        # جلب قائمة المستخدمين لإرسال الإشعار لهم
        users_query = "SELECT telegram_id FROM users"
        users = await connection.fetch(users_query)
        
        # إرسال الإشعار لكل مستخدم
        for user in users:
            tg_id = user['telegram_id']
            try:
                await broadcast_new_notification(tg_id, notification_obj)
            except Exception as e:
                logging.error(f"Error broadcasting public notification to {tg_id}: {e}")

    return notification_id

