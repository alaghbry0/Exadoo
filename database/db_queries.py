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
):
    try:
        await connection.execute("""
            INSERT INTO subscriptions
            (user_id, telegram_id, channel_id, subscription_type_id,
             start_date, expiry_date, subscription_plan_id,
             is_active, source, payment_id, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
        """, user_id, telegram_id, channel_id, subscription_type_id,
            start_date, expiry_date, subscription_plan_id,
            is_active, source, payment_id)

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
    start_date: datetime,
    expiry_date: datetime,
    is_active: bool = True,
    subscription_plan_id: int = None, # اجعلها تقبل None
    payment_id: str = None,          # اجعلها تقبل None
    source: str = "unknown",         # إضافة source
    returning_id: bool = False
):
    try:
        # تأكد من أن جدول subscriptions يسمح بقيم NULL لـ subscription_plan_id و payment_id
        query = """
            INSERT INTO subscriptions
            (telegram_id, channel_id, subscription_type_id, subscription_plan_id,
             start_date, expiry_date, is_active, payment_id,  source, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,  NOW(), NOW())
        """
        params = [
            telegram_id, channel_id, subscription_type_id, subscription_plan_id, # يمكن أن يكون None
            start_date, expiry_date, is_active, payment_id,  source  # يمكن أن يكون None
        ]

        if returning_id:
            query += " RETURNING id"
            new_subscription_id = await connection.fetchval(query, *params)
            logging.info(f"✅ Subscription added with ID {new_subscription_id} for user {telegram_id} (Channel: {channel_id}, Source: {source})")
            return new_subscription_id
        else:
            await connection.execute(query, *params)
            logging.info(f"✅ Subscription added for user {telegram_id} (Channel: {channel_id}, Source: {source})")
            return True

    except Exception as e:
        logging.error(f"❌ Error adding subscription for {telegram_id} (Channel: {channel_id}): {e}", exc_info=True)
        if returning_id:
            return None
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
    subscription_plan_id: int | None = None,
    payment_id: str | None = None,
    source: str | None = None
):
    try:
        # بناء جملة SET بشكل ديناميكي لتحديث source فقط إذا تم توفيره
        set_clauses = [
            "subscription_type_id = $1",
            "subscription_plan_id = $2", # سيمرر None كـ NULL إذا كان subscription_plan_id هو None
            "expiry_date = $3",
            "start_date = $4",
            "is_active = $5",
            "payment_id = $6",          # سيمرر None كـ NULL إذا كان payment_id هو None
            "updated_at = NOW()"
        ]
        params = [
            subscription_type_id, subscription_plan_id, new_expiry_date,
            start_date, is_active, payment_id
        ]

        if source: # فقط قم بتحديث source إذا تم توفيره، وإلا اتركه كما هو
            set_clauses.append(f"source = ${len(params) + 1}")
            params.append(source)

        query = f"""
            UPDATE subscriptions SET
                {', '.join(set_clauses)}
            WHERE telegram_id = ${len(params) + 1} AND channel_id = ${len(params) + 2}
        """
        params.extend([telegram_id, channel_id])

        await connection.execute(query, *params)
        logging.info(f"✅ Subscription updated for {telegram_id} (Channel: {channel_id})" + (f" Source: {source}" if source else ""))
        return True

    except Exception as e:
        logging.error(f"❌ Error updating subscription for {telegram_id} (Channel: {channel_id}): {e}", exc_info=True)
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


# helpers.py (أو داخل نفس الملف قبل الـ endpoint)

async def cancel_subscription_db(
    connection,
    subscription_id: int,
    cancellation_time: datetime,
    reason_source: str = "admin_canceled"
) -> Optional[int]:
    """
    يُلغي الاشتراك المحدد عبر الـ subscription_id:
    - يضبط is_active = FALSE
    - يحدّث expiry_date و source و updated_at
    - يرجع الـ id إذا نجح، أو None إن لم يكن هناك صف نشط
    """
    try:
        updated_id = await connection.fetchval(
            """
            UPDATE subscriptions
            SET
                is_active    = FALSE,
                expiry_date  = $1,
                source       = CASE
                                   WHEN source IS NULL THEN $2
                                   ELSE source || '_canceled'
                               END,
                updated_at   = NOW()
            WHERE id = $3
              AND is_active = TRUE
            RETURNING id;
            """,
            cancellation_time,
            reason_source,
            subscription_id
        )
        if updated_id:
            logging.info(f"✅ cancel_subscription_db: subscription_id={updated_id} canceled.")
        else:
            logging.info(f"ℹ️ cancel_subscription_db: no active row for subscription_id={subscription_id}.")
        return updated_id

    except Exception as e:
        logging.error(f"❌ cancel_subscription_db error for subscription_id={subscription_id}: {e}", exc_info=True)
        return None


async def delete_scheduled_tasks_for_subscription(
        connection,
        telegram_id: int,
        channel_ids: list  # قائمة بـ IDs القنوات (الرئيسية والفرعية)
):
    """
    Deletes 'remove_user' scheduled tasks for the given user and channel IDs.
    """
    if not channel_ids:
        return True
    try:
        await connection.execute(
            """
            DELETE FROM scheduled_tasks
            WHERE task_type = 'remove_user'
              AND telegram_id = $1
              AND channel_id = ANY($2::bigint[])
            """,
            telegram_id,
            channel_ids
        )
        logging.info(f"🧹 Scheduled 'remove_user' tasks deleted for user {telegram_id} and channels {channel_ids}.")
        return True
    except Exception as e:
        logging.error(f"❌ Error deleting scheduled tasks for user {telegram_id}, channels {channel_ids}: {e}",
                      exc_info=True)
        return False


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
    🔹 جلب اشتراكات المستخدم الفعلية مع رابط الدعوة العام للقناة الرئيسية.
    """
    try:
        # 🌟 [الاستعلام المعدل] 🌟
        # نقوم بـ JOIN مع subscription_type_channels حيث is_main=TRUE
        # ونربط بين subscription_type_id في جدول الاشتراكات والجدول الجديد
        subscriptions = await connection.fetch("""
            SELECT 
                s.subscription_type_id, 
                s.start_date,
                s.expiry_date, 
                s.is_active,
                st.name AS subscription_name,
                -- جلب رابط الدعوة من القناة الرئيسية المرتبطة بنوع الاشتراك
                stc.invite_link
            FROM 
                subscriptions s
            JOIN 
                subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN 
                subscription_type_channels stc ON st.id = stc.subscription_type_id AND stc.is_main = TRUE
            WHERE 
                s.telegram_id = $1
        """, telegram_id)

        return subscriptions
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب اشتراكات المستخدم {telegram_id}: {e}", exc_info=True)
        return []


async def record_payment(
        conn,
        telegram_id: int,
        subscription_plan_id: int,
        payment_token: str,
        amount: Optional[Decimal] = None,
        status: str = 'pending',
        payment_method: str = 'USDT (TON)',
        currency: Optional[str] = 'USDT',
        tx_hash: Optional[str] = None,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
        user_wallet_address: Optional[str] = None
) -> Optional[dict]:
    """
    دالة موحدة ومرنة لتسجيل أي نوع من الدفعات.
    """
    query = """
    INSERT INTO payments (
        user_id, telegram_id, subscription_plan_id, amount, amount_received, payment_token, 
        status, payment_method, currency, tx_hash, username, full_name, 
        user_wallet_address, created_at
    ) VALUES (
        $1, $1, $2, $3, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW()
    )
    ON CONFLICT (payment_token) DO UPDATE 
    SET 
        tx_hash = COALESCE(EXCLUDED.tx_hash, payments.tx_hash), 
        status = EXCLUDED.status,
        updated_at = NOW()
    RETURNING *;
    """
    try:
        final_amount = amount if amount is not None else Decimal('0.0')

        payment_record = await conn.fetchrow(
            query,
            telegram_id, subscription_plan_id, final_amount, payment_token,
            status, payment_method, currency, tx_hash, username,
            full_name, user_wallet_address
        )

        if not payment_record:
            raise Exception("Failed to record or retrieve payment from database.")

        logging.info(f"✅ Payment recorded/updated for token {payment_token} with method '{payment_method}'.")

        payment_dict = dict(payment_record)
        if 'amount_received' in payment_dict and payment_dict['amount_received'] is not None and not isinstance(
                payment_dict['amount_received'], Decimal):
            payment_dict['amount_received'] = Decimal(payment_dict['amount_received'])

        return payment_dict

    except Exception as e:
        logging.error(f"❌ Error in record_payment for token {payment_token}: {e}", exc_info=True)
        return None

# --- دالة منفصلة لمدفوعات النجوم (Stars Payments) ---
async def record_telegram_stars_payment(
        conn,
        telegram_id: int,
        plan_id: int,
        payment_id: str, # tx_hash
        payment_token: str,
        amount: int,
        username: Optional[str] = None,
        full_name: Optional[str] = None
) -> Optional[dict]:
    """
    تسجل دفعة نجوم تليجرام. مصممة خصيصًا لمعالج دفع النجوم.
    """
    query = """
    INSERT INTO payments (
        telegram_id, subscription_plan_id, amount_received, payment_token,
        status, payment_method, currency, tx_hash, username, full_name,
        created_at
    ) VALUES (
        $1, $2, $3, $4, 'pending', 'Telegram Stars', 'Stars', $5, $6, $7, NOW()
    )
    ON CONFLICT (payment_token) DO UPDATE
    SET
        tx_hash = EXCLUDED.tx_hash,
        status = 'pending',
        updated_at = NOW()  -- <-- هذا الآن سيعمل بعد تعديل الجدول
    RETURNING *;
    """
    try:
        payment_record = await conn.fetchrow(
            query,
            telegram_id, plan_id, Decimal(amount), payment_token,
            payment_id, username, full_name
        )

        if not payment_record:
            raise Exception("Failed to record or retrieve Telegram Stars payment.")

        logging.info(f"✅ Stars payment recorded/updated for token {payment_token}.")
        return dict(payment_record)

    except Exception as e:
        logging.error(f"❌ Error in record_telegram_stars_payment for token {payment_token}: {e}", exc_info=True)
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
    جلب سجل دفع من قاعدة البيانات بناءً على payment_token.
    تم تحديث هذه الدالة لتشمل عمود 'status' وإزالة الشرط المسبق على الحالة.
    """
    try:
        # لاحظ إضافة 'status' و 'id' إلى جملة SELECT وإزالة "AND status = 'pending'"
        sql = """
            SELECT id, telegram_id, subscription_plan_id, payment_token, 
                   username, full_name, user_wallet_address, amount, status
            FROM payments
            WHERE TRIM(payment_token) = TRIM($1)
            LIMIT 1;
        """
        row = await conn.fetchrow(sql, payment_token)
        if row:
            logging.info(f"✅ تم العثور على سجل دفع لـ payment_token: {payment_token} (الحالة: {row['status']})")
            return dict(row)
        else:
            # هذه ليست رسالة تحذير بالضرورة، قد يكون التوكن من معاملة لا علاقة لها بالدفعات
            logging.info(f"ℹ️ لم يتم العثور على سجل دفع مطابق لـ payment_token: {payment_token}")
            return None
    except Exception as e:
        logging.error(f"❌ فشل في جلب سجل الدفع: {e}", exc_info=True)
        return None


async def record_incoming_transaction(
        conn,
        txhash: str,
        sender: str,
        amount: Decimal,
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

async def update_payment_status_to_manual_check(conn, payment_token: str, error_message: str):
    """
    تحديث حالة الدفع للإشارة إلى أنه يحتاج لمراجعة يدوية بعد فشل تفعيل الاشتراك.
    """
    try:
        await conn.execute(
            """
            UPDATE payments
            SET status = 'manual_check', error_message = $1, processed_at = NOW()
            WHERE payment_token = $2
            """,
            f"Subscription activation failed: {error_message}",
            payment_token
        )
        logging.warning(f"⚠️ تم تحديد الدفعة {payment_token} على أنها تحتاج لمراجعة يدوية.")
    except Exception as e:
        logging.error(f"❌ فشل تحديث حالة الدفع إلى 'manual_check' لـ {payment_token}: {e}")


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