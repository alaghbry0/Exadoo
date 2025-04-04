import json


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
    :param extra_data: بيانات إضافية (مثال: {"subscription_id": 123, "expiry_date": "2025-05-10T12:00:00Z", ...}).
    :param is_public: إذا كان الإشعار عامًا لجميع المستخدمين.
    :param telegram_ids: قائمة بمعرفات تليجرام للمستخدمين المستهدفين (في حالة الإشعار الخاص).
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

    if not is_public:
        # في حالة الإشعار الخاص
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
    else:
        # في حالة الإشعار العام: ربطه بجميع المستخدمين من جدول users
        insert_query = """
            INSERT INTO user_notifications (telegram_id, notification_id)
            SELECT telegram_id, $1 FROM users
        """
        await connection.execute(insert_query, notification_id)

    return notification_id
