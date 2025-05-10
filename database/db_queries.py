import asyncpg
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا
from config import DATABASE_CONFIG
import pytz
import logging
from decimal import Decimal

from typing import Optional, Union


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# ----------------- 🔹 إدارة المستخدمين ----------------- #
async def add_user(connection, telegram_id, username=None, full_name=None, wallet_app=None):
    """
    إضافة مستخدم جديد أو تحديث بيانات مستخدم موجود.
    يتم هنا استخدام عبارة ON CONFLICT لتحديث الحقول في حالة وجود المستخدم مسبقًا.
    """
    try:
        await connection.execute("""
            INSERT INTO users (telegram_id, username, full_name, wallet_app)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                wallet_app = EXCLUDED.wallet_app
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


async def get_user_db_id_by_telegram_id(conn, telegram_id: int) -> Optional[int]:
    """Fetches the primary key 'id' from the 'users' table for a given telegram_id."""
    user_record = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    return user_record['id'] if user_record else None


async def get_active_subscription_types(conn) -> list:
    """Fetches all active subscription types (managed channels)."""
    return await conn.fetch("SELECT id, channel_id, name FROM subscription_types WHERE is_active = TRUE ORDER BY id")


# في db_queries.py (إذا لم تكن موجودة بالفعل أو بشكل مشابه)
async def get_subscription_type_details_by_id(conn, sub_type_id: int):
    """Fetches details for a specific subscription_type_id."""
    return await conn.fetchrow("SELECT id, channel_id FROM subscription_types WHERE id = $1", sub_type_id)


async def add_pending_subscription(
        connection: asyncpg.Connection,
        user_db_id: int,
        telegram_id: int,
        channel_id: int,
        subscription_type_id: int
) -> bool:
    """
    يضيف اشتراكًا معلقًا للمراجعة.
    يستخدم ON CONFLICT لتجنب التكرار.
    يعود True إذا تم إدراج صف جديد، False إذا كان السجل موجودًا بالفعل (بسبب ON CONFLICT).
    """
    try:
        # 🔴 المشكلة الأولى: استدعاء INSERT ... RETURNING id مرتين
        result = await connection.execute(  # <-- الاستدعاء الأول
            """
            INSERT INTO pending_subscriptions (user_db_id, telegram_id, channel_id, subscription_type_id, found_at, status)
            VALUES ($1, $2, $3, $4, NOW(), 'pending')
            ON CONFLICT (telegram_id, channel_id) DO NOTHING
            RETURNING id; 
            """,
            user_db_id,
            telegram_id,
            channel_id,
            subscription_type_id,
        )

        # 🔴 المشكلة الثانية: هذا الشرط غير دقيق ويعتمد على سلسلة نصية
        if result and " 0 0" not in result:  # طريقة بسيطة للتحقق, قد تحتاج لتحسين
            # 🔴 المشكلة الثالثة: إذا كان الشرط أعلاه صحيحًا، يتم استدعاء نفس جملة INSERT مرة أخرى!
            record_id = await connection.fetchval(  # <-- الاستدعاء الثاني لنفس جملة INSERT
                """
                INSERT INTO pending_subscriptions (user_db_id, telegram_id, channel_id, subscription_type_id, found_at, status)
                VALUES ($1, $2, $3, $4, NOW(), 'pending')
                ON CONFLICT (telegram_id, channel_id) DO NOTHING
                RETURNING id;
                """,
                user_db_id, telegram_id, channel_id, subscription_type_id
            )
            return record_id is not None  # تم الإدراج إذا أعيد id

    except Exception as e:
        logging.error(
            f"❌ Error adding pending subscription for user_db_id {user_db_id} (TG: {telegram_id}), channel {channel_id}: {e}",
            exc_info=True
        )
        return False
    return False


async def add_subscription_for_legacy(
    connection: asyncpg.Connection, # الأفضل تحديد نوع الاتصال
    user_id: int,
    telegram_id: int,
    channel_id: int,
    subscription_type_id: int,
    start_date: datetime,
    expiry_date: datetime,
    subscription_plan_id: Optional[int] = None,
    is_active: bool = True,
    source: Optional[str] = None,
    payment_id: Optional[str] = None,
    invite_link: Optional[str] = None
):
    try:
        await connection.execute("""
            INSERT INTO subscriptions
            (user_id, telegram_id, channel_id, subscription_type_id,
             start_date, expiry_date, subscription_plan_id,
             is_active, source, payment_id, invite_link, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
        """, user_id, telegram_id, channel_id, subscription_type_id,
            start_date, expiry_date, subscription_plan_id,
            is_active, source, payment_id, invite_link)

        logging.info(f"✅ Subscription added for user_id {user_id} (TG: {telegram_id}, Channel: {channel_id}, Source: {source})")
        return True

    except Exception as e:
        logging.error(f"❌ Error adding subscription for user_id {user_id} (TG: {telegram_id}): {e}", exc_info=True) # أضفت exc_info=True لتفاصيل أفضل
        return False


# ----------------- 🔹 إدارة الاشتراكات ----------------- #

async def add_subscription(
        connection,
        telegram_id: int,
        channel_id: int,
        subscription_type_id: int,
        subscription_plan_id: int,
        start_date: datetime,
        expiry_date: datetime,
        is_active: bool = True,
        payment_id: str = None  # <-- إضافة payment_id كمعامل اختياري
):
    try:
        await connection.execute("""
            INSERT INTO subscriptions 
            (telegram_id, channel_id, subscription_type_id, subscription_plan_id, start_date, expiry_date, is_active, payment_id, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
        """, telegram_id, channel_id, subscription_type_id, subscription_plan_id, start_date, expiry_date, is_active,
                                 payment_id)

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
        subscription_plan_id: int,
        new_expiry_date: datetime,
        start_date: datetime,
        is_active: bool = True,
        payment_id: str = None,
        invite_link: str = None  # <-- إضافة invite_link
):
    try:
        if payment_id or invite_link:  # ✅ تحديث إذا كان هناك invite_link أو payment_id
            await connection.execute("""
                UPDATE subscriptions SET
                    subscription_type_id = $1,
                    subscription_plan_id = $2,                
                    expiry_date = $3,
                    start_date = $4,
                    is_active = $5,
                    payment_id = $6,
                    invite_link = $7,
                    updated_at = NOW()
                WHERE telegram_id = $8 AND channel_id = $9
            """, subscription_type_id, subscription_plan_id, new_expiry_date,
                                     start_date, is_active, payment_id, invite_link, telegram_id, channel_id)
        else:  # ✅ تحديث بدون تعديل `payment_id` أو `invite_link`
            await connection.execute("""
                UPDATE subscriptions SET
                    subscription_type_id = $1,
                    subscription_plan_id = $2,                 
                    expiry_date = $3,
                    start_date = $4,
                    is_active = $5,
                    updated_at = NOW()
                WHERE telegram_id = $6 AND channel_id = $7
            """, subscription_type_id, new_expiry_date, start_date,
                                     is_active, telegram_id, channel_id)

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
                s.start_date,
                s.expiry_date, 
                s.is_active,
                s.invite_link,
                st.name AS subscription_name
            FROM subscriptions s
            JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE s.telegram_id = $1
        """, telegram_id)

        return subscriptions
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب اشتراكات المستخدم {telegram_id}: {e}")
        return []


# db_queries.py - record_payment function signature
async def record_payment(
        conn,
        telegram_id,
        user_wallet_address,
        amount,
        subscription_plan_id,
        username=None,
        full_name=None,
        payment_token=None  # استخدام payment_token فقط
):
    """تسجيل بيانات الدفع مع payment_token الفريد"""
    try:
        sql = """
            INSERT INTO payments (
                user_id, 
                subscription_plan_id, 
                amount, 
                telegram_id, 
                username, 
                full_name, 
                user_wallet_address, 
                status, 
                payment_token
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8)
            RETURNING payment_token, amount;
        """
        result = await conn.fetchrow(
            sql,
            telegram_id,
            subscription_plan_id,
            amount,
            telegram_id,
            username,
            full_name,
            user_wallet_address,
            payment_token
        )

        if result:
            logging.info(f"✅ تم تسجيل الدفعة: {payment_token}")
            return {
                "payment_token": payment_token,
                "amount": result["amount"]
            }
        else:
            logging.error("❌ فشل إدخال الدفعة")
            return None

    except Exception as e:
        logging.error(f"❌ خطأ في record_payment: {str(e)}")
        return None


async def update_payment_with_txhash(
        conn,
        payment_token: str,
        tx_hash: str,
        amount_received: Decimal,
        status: str = "completed",
        error_message: Optional[str] = None
) -> Optional[dict]:
    """
    تحديث سجل الدفع مع تفاصيل جديدة وتحديث حالة المعاملة في incoming_transactions
    """
    try:
        # بدء transaction واحدة لضمان التزامن
        async with conn.transaction():
            # 1. تحديث جدول payments
            payment_query = """
                UPDATE payments
                SET 
                    tx_hash = $1,
                    amount_received = $2,
                    status = $3,
                    error_message = $4,
                    processed_at = NOW()
                WHERE payment_token = $5
                RETURNING *;
            """
            payment_row = await conn.fetchrow(
                payment_query,
                tx_hash,
                amount_received,
                status,
                error_message,
                payment_token
            )

            if not payment_row:
                logging.error(f"❌ لم يتم العثور على دفعة بالـ token: {payment_token}")
                return None

            # 2. تحديث جدول incoming_transactions
            incoming_query = """
                UPDATE incoming_transactions
                SET processed = TRUE
                WHERE txhash = $1
                RETURNING txhash;
            """
            incoming_row = await conn.fetchrow(incoming_query, tx_hash)

            if not incoming_row:
                logging.warning(f"⚠️ لم يتم العثور على معاملة واردة بالـ txhash: {tx_hash}")

            return dict(payment_row)

    except Exception as e:
        logging.error(f"❌ فشل تحديث الدفعة والمعاملة: {str(e)}", exc_info=True)
        return None


async def fetch_pending_payment_by_payment_token(conn, payment_token: str) -> Optional[dict]:
    """
    جلب سجل دفع معلق من قاعدة البيانات بناءً على payment_token فقط.
    """
    try:
        sql = """
            SELECT telegram_id, subscription_plan_id, payment_token, username, full_name, user_wallet_address, amount
            FROM payments
            WHERE TRIM(payment_token) = TRIM($1)
              AND status = 'pending'
            LIMIT 1;
        """
        row = await conn.fetchrow(sql, payment_token)
        if row:
            logging.info(f"✅ تم العثور على سجل دفع معلق لـ payment_token: {payment_token}")
            return dict(row)
        else:
            logging.warning(f"⚠️ لم يتم العثور على سجل دفع معلق لـ payment_token: {payment_token}")
            return None
    except Exception as e:
        logging.error(f"❌ فشل في جلب سجل الدفع المعلق: {e}", exc_info=True)
        return None


async def record_incoming_transaction(
        conn,
        txhash: str,
        sender: str,
        amount: float,
        payment_token: Optional[str] = None,
        memo: Optional[str] = None
):
    """
    تسجيل المعاملة الواردة في جدول incoming_transactions مع التوقيت المصحح
    """
    try:
        await conn.execute('''
            INSERT INTO incoming_transactions (
                txhash, 
                sender_address, 
                amount, 
                payment_token, 
                processed, 
                memo,
                received_at  -- إضافة القيمة يدويًا
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                (NOW() AT TIME ZONE 'UTC' + INTERVAL '3 hours')::timestamp  -- حساب التوقيت الصحيح
            )
            ON CONFLICT (txhash) DO NOTHING
        ''',
                           txhash,
                           sender,
                           amount,
                           payment_token,
                           False,
                           memo)
        logging.info(f"✅ تم تسجيل المعاملة {txhash}")
    except Exception as e:
        logging.error(f"❌ فشل تسجيل المعاملة {txhash}: {str(e)}")


async def get_unread_notifications_count(connection, telegram_id: int) -> int:
    """
    إرجاع عدد الإشعارات غير المقروءة للمستخدم.
    """
    try:
        query = """
            SELECT COUNT(*) AS unread_count
            FROM user_notifications
            WHERE telegram_id = $1 AND read_status = FALSE;
        """
        result = await connection.fetchrow(query, telegram_id)
        return result["unread_count"] if result else 0
    except Exception as e:
        logging.error(f"Error fetching unread notifications for {telegram_id}: {e}")
        return 0