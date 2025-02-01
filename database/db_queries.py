import asyncpg
from datetime import datetime
from config import DATABASE_CONFIG
import logging


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# ----------------- 🔹 إدارة المستخدمين ----------------- #
async def add_user(connection, telegram_id, username=None, full_name=None, wallet_app=None):
    """إضافة مستخدم جديد أو تحديث بيانات مستخدم موجود."""
    try:
        await connection.execute("""
            INSERT INTO users (telegram_id, username, full_name, wallet_app)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = $2, full_name = $3, wallet_app = $4
        """, telegram_id, username, full_name, wallet_app)
        logging.info(f"User {telegram_id} added/updated successfully.")
        return True
    except Exception as e:
        logging.error(f"Error adding/updating user {telegram_id}: {e}")
        return False


async def get_user(connection, telegram_id: int):
    """
    جلب بيانات المستخدم من قاعدة البيانات باستخدام Telegram ID.
    """
    try:
        user = await connection.fetchrow("""
            SELECT telegram_id, username, full_name, wallet_address, wallet_app, 
                   CASE 
                       WHEN wallet_address IS NOT NULL THEN 'connected'
                       ELSE 'disconnected'
                   END AS wallet_status
            FROM users
            WHERE telegram_id = $1
        """, telegram_id)

        if user:
            logging.info(f"✅ User {telegram_id} found in database.")
        else:
            logging.warning(f"⚠️ User {telegram_id} not found in database.")
        return user
    except Exception as e:
        logging.error(f"❌ Error fetching user {telegram_id}: {e}")
        return None


# ----------------- 🔹 إدارة الاشتراكات ----------------- #

async def add_subscription(
    connection,
    telegram_id: int,
    channel_id: int,
    subscription_type_id: int,
    start_date: datetime,  # <-- أضف هذا البارامتر
    expiry_date: datetime,
    is_active: bool = True
):
    try:
        await connection.execute("""
            INSERT INTO subscriptions 
            (telegram_id, channel_id, subscription_type_id, start_date, expiry_date, is_active)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, telegram_id, channel_id, subscription_type_id, start_date, expiry_date, is_active)
        logging.info(f"✅ Subscription added for user {telegram_id}")
        return True
    except Exception as e:
        logging.error(f"❌ Error adding subscription: {e}")
        return False

# 1. تعديل دالة update_subscription (إزالة التعليقات الداخلية)
async def update_subscription(
    connection,
    telegram_id: int,
    channel_id: int,
    subscription_type_id: int,
    new_expiry_date: datetime,
    start_date: datetime,
    is_active: bool = True
):
    try:
        await connection.execute("""
            UPDATE subscriptions SET
                subscription_type_id = $1,
                expiry_date = $2,
                start_date = $3,
                is_active = $4
            WHERE telegram_id = $5 AND channel_id = $6
        """, subscription_type_id, new_expiry_date, start_date, is_active, telegram_id, channel_id)
        logging.info(f"✅ Subscription updated for {telegram_id}")
        return True
    except Exception as e:
        logging.error(f"❌ Error updating subscription: {e}")
        return False

async def get_subscription(connection, telegram_id: int, channel_id: int):
    """
    جلب الاشتراك الحالي للمستخدم.
    """
    try:
        subscription = await connection.fetchrow("""
            SELECT * FROM subscriptions
            WHERE telegram_id = $1 AND channel_id = $2
        """, telegram_id, channel_id)

        if subscription and subscription['expiry_date'] < datetime.now():
            await connection.execute("""
                UPDATE subscriptions
                SET is_active = FALSE
                WHERE id = $1
            """, subscription['id'])
            logging.info(f"⚠️ Subscription for user {telegram_id} in channel {channel_id} marked as inactive.")
            return {**subscription, 'is_active': False}

        return subscription
    except Exception as e:
        logging.error(f"❌ Error retrieving subscription for user {telegram_id} in channel {channel_id}: {e}")
        return None


async def deactivate_subscription(connection, telegram_id: int, channel_id: int = None):
    """
    تعطيل جميع الاشتراكات أو اشتراك معين للمستخدم.
    """
    try:
        query = """
            UPDATE subscriptions
            SET is_active = FALSE
            WHERE telegram_id = $1
        """
        params = [telegram_id]

        if channel_id:
            query += " AND channel_id = $2"
            params.append(channel_id)

        await connection.execute(query, *params)
        logging.info(f"✅ Subscription(s) for user {telegram_id} deactivated.")
        return True
    except Exception as e:
        logging.error(f"❌ Error deactivating subscription(s) for user {telegram_id}: {e}")
        return False


# ----------------- 🔹 إدارة المهام المجدولة ----------------- #

async def add_scheduled_task(connection, task_type: str, telegram_id: int, channel_id: int, execute_at: datetime,
                             clean_up: bool = True):
    """
    إضافة مهمة مجدولة.
    """
    try:
        if clean_up:
            await connection.execute("""
                DELETE FROM scheduled_tasks
                WHERE telegram_id = $1 AND channel_id = $2 AND task_type = $3
            """, telegram_id, channel_id, task_type)

        await connection.execute("""
            INSERT INTO scheduled_tasks (task_type, telegram_id, channel_id, execute_at, status)
            VALUES ($1, $2, $3, $4, 'pending')
        """, task_type, telegram_id, channel_id, execute_at)

        logging.info(f"✅ Scheduled task '{task_type}' for user {telegram_id} and channel {channel_id} at {execute_at}.")
        return True
    except Exception as e:
        logging.error(
            f"❌ Error adding scheduled task '{task_type}' for user {telegram_id} and channel {channel_id}: {e}")
        return False


async def get_pending_tasks(connection, channel_id: int = None):
    """
    جلب المهام المعلقة التي يجب تنفيذها.
    """
    try:
        query = """
            SELECT * FROM scheduled_tasks
            WHERE status = 'pending'
              AND execute_at <= NOW()
        """
        params = []

        if channel_id:
            query += " AND channel_id = $1"
            params.append(channel_id)

        tasks = await connection.fetch(query, *params)

        logging.info(f"✅ Retrieved {len(tasks)} pending tasks (channel_id: {channel_id}).")
        return tasks
    except Exception as e:
        logging.error(f"❌ Error retrieving pending tasks (channel_id: {channel_id}): {e}")
        return []


async def update_task_status(connection, task_id: int, status: str):
    """
    تحديث حالة المهمة المجدولة.
    """
    try:
        await connection.execute("""
            UPDATE scheduled_tasks
            SET status = $1
            WHERE id = $2
        """, status, task_id)
        logging.info(f"✅ Task {task_id} status updated to {status}.")
        return True
    except Exception as e:
        logging.error(f"❌ Error updating task {task_id} status to {status}: {e}")
        return False


async def get_user_subscriptions(connection, telegram_id: int):
    """
    🔹 جلب اشتراكات المستخدم الفعلية مع تاريخ البدء
    """
    try:
        subscriptions = await connection.fetch("""
            SELECT 
                s.subscription_type_id, 
                s.start_date,  -- <-- إضافة هذا الحقل
                s.expiry_date, 
                s.is_active,
                st.name AS subscription_name,
                st.price
            FROM subscriptions s
            JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE s.telegram_id = $1
        """, telegram_id)

        return subscriptions
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب اشتراكات المستخدم {telegram_id}: {e}")
        return []