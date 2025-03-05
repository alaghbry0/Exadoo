import asyncpg
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا
from config import DATABASE_CONFIG
import pytz
import logging
from typing import Optional

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
    start_date: datetime,
    expiry_date: datetime,
    is_active: bool = True,
    payment_id: str = None  # <-- إضافة payment_id كمعامل اختياري
):
    try:
        await connection.execute("""
            INSERT INTO subscriptions 
            (telegram_id, channel_id, subscription_type_id, start_date, expiry_date, is_active, payment_id, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        """, telegram_id, channel_id, subscription_type_id, start_date, expiry_date, is_active, payment_id)

        logging.info(f"✅ Subscription added for user {telegram_id} (Channel: {channel_id})")
        return True

    except Exception as e:
        logging.error(f"❌ Error adding subscription for {telegram_id}: {e}")
        return False


# 1. تعديل دالة update_subscription (إزالة التعليقات الداخلية)
async def update_subscription(
    connection,
    telegram_id: int,
    channel_id: int,
    subscription_type_id: int,
    new_expiry_date: datetime,
    start_date: datetime,
    is_active: bool = True,
    payment_id: str = None  # <-- إضافة payment_id كمعامل اختياري
):
    try:
        if payment_id:  # ✅ تحديث payment_id فقط إذا كان موجودًا
            await connection.execute("""
                UPDATE subscriptions SET
                    subscription_type_id = $1,
                    expiry_date = $2,
                    start_date = $3,
                    is_active = $4,
                    payment_id = $5,
                    updated_at = NOW()
                WHERE telegram_id = $6 AND channel_id = $7
            """, subscription_type_id, new_expiry_date, start_date, is_active, payment_id, telegram_id, channel_id)
        else:  # ✅ تحديث بدون تعديل `payment_id`
            await connection.execute("""
                UPDATE subscriptions SET
                    subscription_type_id = $1,
                    expiry_date = $2,
                    start_date = $3,
                    is_active = $4,
                    updated_at = NOW()
                WHERE telegram_id = $5 AND channel_id = $6
            """, subscription_type_id, new_expiry_date, start_date, is_active, telegram_id, channel_id)

        logging.info(f"✅ Subscription updated for {telegram_id} (Channel: {channel_id})")
        return True

    except Exception as e:
        logging.error(f"❌ Error updating subscription for {telegram_id}: {e}")
        return False

async def get_subscription(connection, telegram_id: int, channel_id: int):
    """
    🔹 جلب الاشتراك الحالي للمستخدم، مع التأكد من أن `expiry_date` هو `timezone-aware`.
    """
    try:
        subscription = await connection.fetchrow("""
            SELECT * FROM subscriptions
            WHERE telegram_id = $1 AND channel_id = $2
        """, telegram_id, channel_id)

        if subscription:
            expiry_date = subscription['expiry_date']

            # ✅ التأكد من أن `expiry_date` يحتوي على timezone
            if expiry_date.tzinfo is None:
                expiry_date = expiry_date.replace(tzinfo=timezone.utc)

            # ✅ مقارنة `expiry_date` مع الوقت الحالي الصحيح
            now_utc = datetime.now(timezone.utc)
            if expiry_date < now_utc:
                await connection.execute("""
                    UPDATE subscriptions
                    SET is_active = FALSE
                    WHERE id = $1
                """, subscription['id'])
                logging.info(f"⚠️ Subscription for user {telegram_id} in channel {channel_id} marked as inactive.")

                return {**subscription, 'expiry_date': expiry_date, 'is_active': False}

            return {**subscription, 'expiry_date': expiry_date}

        return None  # لا يوجد اشتراك
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
    try:
        # تحويل execute_at إلى توقيت UTC إذا كان naive
        if execute_at.tzinfo is None:
            execute_at = execute_at.replace(tzinfo=timezone.utc)
        else:
            execute_at = execute_at.astimezone(timezone.utc)

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
    🔹 جلب المهام المعلقة التي يجب تنفيذها، مع التأكد من ضبط `execute_at` بتوقيت UTC.
    """
    try:
        query = """
            SELECT * FROM scheduled_tasks
            WHERE status = 'pending'
        """
        params = []

        if channel_id:
            query += " AND channel_id = $1"
            params.append(channel_id)

        # 🔹 جلب المهام بدون فلترة `execute_at` داخل SQL (لتجنب مشاكل التوقيت)
        tasks = await connection.fetch(query, *params)

        # 🔹 التحقق من توقيت كل مهمة داخل Python
        current_time = datetime.now(timezone.utc)
        pending_tasks = []

        for task in tasks:
            execute_at = task['execute_at']

            # ✅ التأكد من أن `execute_at` هو `timezone-aware`
            if execute_at.tzinfo is None:
                execute_at = execute_at.replace(tzinfo=timezone.utc)

            # ✅ إضافة المهمة إذا كان وقتها قد حان أو تأخر
            if execute_at <= current_time:
                pending_tasks.append({**task, 'execute_at': execute_at})

        logging.info(f"✅ Retrieved {len(pending_tasks)} pending tasks (channel_id: {channel_id}).")
        return pending_tasks

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
                
            FROM subscriptions s
            JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE s.telegram_id = $1
        """, telegram_id)

        return subscriptions
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب اشتراكات المستخدم {telegram_id}: {e}")
        return []


# db_queries.py - record_payment function signature
async def record_payment(conn, telegram_id, user_wallet_address, amount, subscription_plan_id, username=None, full_name=None, order_id=None): # ✅ إضافة order_id كمعامل
    """تسجيل بيانات الدفع والمستخدم في قاعدة البيانات كدفعة معلقة."""
    try:
        sql = """
            INSERT INTO payments (user_id, subscription_plan_id, amount, payment_date, telegram_id, username, full_name, user_wallet_address, status, order_id) -- ✅ إضافة عمود order_id
            VALUES ($1, $2, $3, NOW(), $4, $5, $6, $7, 'pending', $8) -- ✅ إضافة $8 (order_id) إلى قائمة القيم
            RETURNING payment_id, payment_date;
        """
        result = await conn.fetchrow(sql, telegram_id, subscription_plan_id, amount, telegram_id, username, full_name, user_wallet_address, order_id) # ✅ تمرير order_id إلى الاستعلام
        if result:
            payment_id, payment_date = result['payment_id'], result['payment_date']
            logging.info(f"✅ تم تسجيل دفعة معلقة جديدة بنجاح في قاعدة البيانات. معرف الدفع: {payment_id}, تاريخ الدفع: {payment_date}, order_id: {order_id}") # ✅ تسجيل order_id في السجل
            return {"payment_id": payment_id, "payment_date": payment_date}
        else:
            logging.error("❌ فشل تسجيل دفعة معلقة في قاعدة البيانات.")
            return None
    except Exception as e:
        logging.error(f"❌ خطأ أثناء تسجيل الدفع في قاعدة البيانات: {e}", exc_info=True)
        return None

async def update_payment_with_txhash(conn, payment_id: str, tx_hash: str) -> Optional[dict]:
    """
    تقوم هذه الدالة بتحديث سجل الدفع في قاعدة البيانات باستخدام payment_id لتسجيل tx_hash
    وتحديث حالة الدفع إلى 'completed' وتحديث حقل 'payment_date' إلى تاريخ ووقت الآن.
    تستخدم اتصال قاعدة البيانات المُمرر `conn` لتنفيذ العملية.
    تُعيد السجل المحدث كقاموس يحتوي على بيانات المستخدم، أو None إذا لم يتم العثور على السجل أو حدث خطأ.
    """
    try:
        row = await conn.fetchrow(
            """
            UPDATE payments
            SET tx_hash = $1,
                status = 'completed',
                payment_date = NOW()
            WHERE payment_id = $2
            RETURNING telegram_id, subscription_plan_id, username, full_name, user_wallet_address, order_id; -- ✅ إرجاع user_wallet_address و order_id
            """,
            tx_hash, payment_id
        )
        if row:
            logging.info(f"✅ تم تحديث سجل الدفع بنجاح للـ payment_id: {payment_id}")
            return dict(row)
        else:
            logging.error(f"❌ لم يتم العثور على سجل الدفع للـ payment_id: {payment_id}")
            return None
    except Exception as e:
        logging.error(f"❌ فشل تحديث سجل الدفع: {e}", exc_info=True)
        return None


async def fetch_pending_payment_by_orderid(conn, order_id: str) -> Optional[dict]:
    """
    تقوم هذه الدالة بجلب سجل دفع معلق من قاعدة البيانات بناءً على orderId فقط.
    يتم تطبيع (trim) للـ orderId لتفادي اختلافات التنسيق.
    """
    try:
        sql = """
            SELECT payment_id, telegram_id, subscription_plan_id, username, full_name, user_wallet_address, order_id, amount
            FROM payments
            WHERE TRIM(order_id) = TRIM($1)
              AND status = 'pending'
            ORDER BY payment_date ASC
            LIMIT 1;
        """
        row = await conn.fetchrow(sql, order_id)
        if row:
            logging.info(f"✅ تم العثور على سجل دفع معلق لـ orderId: {order_id}")
            return dict(row)
        else:
            logging.warning(f"⚠️ لم يتم العثور على سجل دفع معلق لـ orderId: {order_id}")
            return None
    except Exception as e:
        logging.error(f"❌ فشل في جلب سجل الدفع المعلق: {e}", exc_info=True)
        return None

