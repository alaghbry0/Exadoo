# database/db_queries.py

import asyncpg
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا
from config import DATABASE_CONFIG
import pytz
import logging
from decimal import Decimal
import json
from typing import Optional, Union


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


async def upsert_user(connection, telegram_id: int, username: str, full_name: str) -> bool:
    """
    إضافة مستخدم جديد أو تحديث بياناته الحالية (UPSERT) في جدول users.

    Args:
        connection: اتصال قاعدة البيانات.
        telegram_id: معرف المستخدم في تليجرام.
        username: اسم المستخدم في تليجرام.
        full_name: الاسم الكامل للمستخدم.

    Returns:
        True إذا تمت العملية بنجاح, False في حالة حدوث خطأ.
    """
    try:
        query = """
            INSERT INTO users (telegram_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE 
            SET
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name;
        """
        await connection.execute(query, telegram_id, username, full_name)
        logging.info(f"✅ User {telegram_id} upserted successfully.")
        return True
    except Exception as e:
        logging.error(f"❌ Error upserting user {telegram_id}: {e}", exc_info=True)
        return False

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
        *,
        subscription_plan_id: int | None = None,
        payment_id: str | None = None,
        source: str = "unknown",
        payment_token: str | None = None,
        returning_id: bool = False
):
    """
    إضافة سجل اشتراك جديد في قاعدة البيانات.
    """
    try:
        # ✅ جلب user_id من جدول users
        user_row = await connection.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1", telegram_id
        )
        if not user_row:
            raise Exception(f"❌ User with telegram_id {telegram_id} not found in 'users' table.")

        user_id = user_row['id']

        # ✅ بناء الاستعلام بشكل ديناميكي
        columns = [
            "user_id", "telegram_id", "channel_id", "subscription_type_id",
            "start_date", "expiry_date", "is_active", "source"
        ]
        params = [
            user_id, telegram_id, channel_id, subscription_type_id,
            start_date, expiry_date, is_active, source
        ]

        if subscription_plan_id is not None:
            columns.append("subscription_plan_id")
            params.append(subscription_plan_id)

        if payment_id is not None:
            columns.append("payment_id")
            params.append(payment_id)

        if payment_token is not None:
            columns.append("payment_token")
            params.append(payment_token)

        # إضافة created_at و updated_at إلى الأعمدة والمعاملات
        columns.extend(["created_at", "updated_at"])
        # استخدم datetime.now(timezone.utc) لضمان أن التوقيت هو UTC
        current_utc_time = datetime.now(timezone.utc)
        params.extend([current_utc_time, current_utc_time])

        values_placeholders = [f"${i + 1}" for i in range(len(params))]

        query = f"""
            INSERT INTO subscriptions ({', '.join(columns)})
            VALUES ({', '.join(values_placeholders)})
        """

        if returning_id:
            query += " RETURNING id"
            new_subscription_id = await connection.fetchval(query, *params)
            logging.info(
                f"✅ Subscription added with ID {new_subscription_id} for user {telegram_id} (Channel: {channel_id}, Source: {source})"
            )
            return new_subscription_id
        else:
            await connection.execute(query, *params)
            logging.info(
                f"✅ Subscription added for user {telegram_id} (Channel: {channel_id}, Source: {source})"
            )
            return True

    except Exception as e:
        logging.error(f"❌ Error adding subscription for {telegram_id} (Channel: {channel_id}): {e}", exc_info=True)
        return None if returning_id else False


# هذا هو الكود الصحيح الذي يجب أن يكون في ملفك
async def update_subscription(
    connection,
    telegram_id: int,
    channel_id: int,
    subscription_type_id: int,
    new_expiry_date: datetime,
    start_date: datetime,
    is_active: bool = True,
    *,
    subscription_plan_id: int | None = None,
    payment_id: str | None = None,       # <-- يقبل payment_id
    source: str | None = None,           # <-- يقبل source
    payment_token: str | None = None     # <-- يقبل payment_token
):
    """
    تحديث سجل اشتراك موجود.
    """
    try:
        set_clauses = [
            "subscription_type_id = $1",
            "start_date = $2",
            "expiry_date = $3",
            "is_active = $4",
            "updated_at = NOW()"
        ]
        params = [
            subscription_type_id,
            start_date,
            new_expiry_date,
            is_active
        ]

        # بناء الاستعلام بشكل ديناميكي لتجنب المشاكل مع NULL
        if subscription_plan_id is not None:
            params.append(subscription_plan_id)
            set_clauses.append(f"subscription_plan_id = ${len(params)}")
        if payment_id is not None:
            params.append(payment_id)
            set_clauses.append(f"payment_id = ${len(params)}")
        if source is not None:
            params.append(source)
            set_clauses.append(f"source = ${len(params)}")
        if payment_token is not None:
            params.append(payment_token)
            set_clauses.append(f"payment_token = ${len(params)}")

        # إضافة شروط WHERE في النهاية
        params.extend([telegram_id, channel_id])
        query = f"""
            UPDATE subscriptions SET
                {', '.join(set_clauses)}
            WHERE telegram_id = ${len(params) - 1} AND channel_id = ${len(params)}
        """

        await connection.execute(query, *params)
        logging.info(f"✅ Subscription updated for {telegram_id} (Channel: {channel_id})" + (f" Source: {source}" if source else ""))
        return True

    except Exception as e:
        logging.error(f"❌ Error updating subscription for {telegram_id} (Channel: {channel_id}): {e}", exc_info=True)
        return False


async def get_subscription(connection, telegram_id: int, channel_id: int):
    """
    🔹 جلب الاشتراك الحالي للمستخدم، وتحديد ما إذا كان للقناة الرئيسية، وتحديث حالته إذا لزم الأمر.
    """
    try:
        # ⭐ تعديل: أضفنا LEFT JOIN للتحقق مما إذا كانت القناة هي القناة الرئيسية
        query = """
            SELECT 
                s.*, 
                -- نتحقق مما إذا كان channel_id للاشتراك يطابق الـ channel_id الرئيسي لنوع الاشتراك
                (st.channel_id = s.channel_id) AS is_main_channel_subscription
            FROM subscriptions s
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE s.telegram_id = $1 AND s.channel_id = $2
        """
        subscription = await connection.fetchrow(query, telegram_id, channel_id)

        if not subscription:
            return None  # لا يوجد اشتراك

        # --- لا نغير المنطق التالي ---
        # لا يزال من الجيد تحديث الحالة هنا كإجراء وقائي
        expiry_date = subscription['expiry_date']
        is_active = subscription['is_active']

        if expiry_date.tzinfo is None:
            expiry_date = expiry_date.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)

        # إذا كان الاشتراك لا يزال نشطاً في قاعدة البيانات ولكنه منتهي الصلاحية فعلياً
        if is_active and expiry_date < now_utc:
            await connection.execute(
                "UPDATE subscriptions SET is_active = FALSE WHERE id = $1",
                subscription['id']
            )
            logging.info(f"Proactively marked subscription for user {telegram_id} in channel {channel_id} as inactive.")
            # نرجع نسخة محدثة من السجل
            return {**subscription, 'expiry_date': expiry_date, 'is_active': False}

        # نرجع السجل مع تاريخ محدث
        return {**subscription, 'expiry_date': expiry_date}

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

# --- ⭐ تعديل جوهري: تجميع الخصومات القابلة للإلغاء حسب الخصم الأصلي ---
async def find_lapsable_user_discounts_for_type(connection, telegram_id: int, subscription_type_id: int) -> list[dict]:
    """
    Finds all active user discounts for a given subscription type that should be lost on lapse.
    It groups them by the original discount ID.
    Returns a list of records, where each record contains the original discount_id
    and a list of user_discount_ids associated with it.
    Example: [{'original_discount_id': 5, 'user_discount_ids': [101, 102]}]
    """
    user_id = await connection.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    if not user_id:
        return []

    # هذا الاستعلام يربط user_discounts بالخطط، ثم بالأنواع، ثم بالخصومات الأصلية
    # ويقوم بتجميع النتائج حسب الخصم الأصلي
    query = """
        SELECT
            d.id as original_discount_id,
            array_agg(ud.id) as user_discount_ids
        FROM user_discounts ud
        JOIN subscription_plans sp ON ud.subscription_plan_id = sp.id
        JOIN discounts d ON ud.discount_id = d.id
        WHERE ud.user_id = $1
          AND sp.subscription_type_id = $2
          AND ud.is_active = true
          AND d.lose_on_lapse = true
        GROUP BY d.id;
    """
    return await connection.fetch(query, user_id, subscription_type_id)

# --- ⭐ دالة جديدة: للعثور على خصومات المستخدم بناءً على الخصم الأصلي (لا تغيير) ---
async def find_active_user_discounts_by_original_discount(connection, user_id: int, original_discount_id: int) -> list[int]:
    """
    Finds all active user_discounts IDs for a user that were created from a specific original discount.
    """
    query = """
        SELECT id FROM user_discounts
        WHERE user_id = $1 AND discount_id = $2 AND is_active = true;
    """
    results = await connection.fetch(query, user_id, original_discount_id)
    return [record['id'] for record in results] # نرجع قائمة من الأرقام مباشرة
# --- ⭐ 2. تعديل: دالة إلغاء مجموعة من الخصومات ---
async def deactivate_multiple_user_discounts(connection, user_discount_ids: list[int]) -> int:
    """
    Deactivates a list of user discounts by their IDs.
    This is now safer against unique constraint violations.
    """
    if not user_discount_ids:
        return 0
    try:
        # نبدأ معاملة لضمان تنفيذ العمليتين معًا
        async with connection.transaction():
            # الخطوة 1: ابحث عن المستخدمين والخطط للسجلات التي سيتم تحديثها
            user_plan_tuples = await connection.fetch("""
                SELECT user_id, subscription_plan_id FROM user_discounts
                WHERE id = ANY($1) AND is_active = true
            """, user_discount_ids)

            if not user_plan_tuples:
                logging.warning(f"No active user discounts found for IDs {user_discount_ids} to deactivate.")
                return 0

            # الخطوة 2: احذف أي سجلات غير نشطة موجودة مسبقًا وتتطابق مع نفس المستخدمين والخطط
            # هذا يمنع حدوث التعارض في الخطوة التالية
            await connection.executemany("""
                DELETE FROM user_discounts
                WHERE user_id = $1 AND subscription_plan_id = $2 AND is_active = false
            """, [(r['user_id'], r['subscription_plan_id']) for r in user_plan_tuples])

            # الخطوة 3: الآن قم بتحديث السجلات النشطة بأمان
            result = await connection.execute(
                "UPDATE user_discounts SET is_active = false WHERE id = ANY($1) AND is_active = true",
                user_discount_ids
            )

            # استخراج عدد الصفوف المحدثة
            count_str = result.split(" ")[1]
            deactivated_count = int(count_str)

            logging.info(f"✅ Successfully deactivated {deactivated_count} user discounts.")
            return deactivated_count

    except Exception as e:
        # تحقق مما إذا كان الخطأ هو UniqueViolationError. إذا كان كذلك، فقد يعني أن هناك حالة سباق (race condition)
        # لم يتم التعامل معها. تسجيل الخطأ لا يزال مهمًا.
        if isinstance(e, asyncpg.exceptions.UniqueViolationError):
            logging.error(
                f"❌ A unique violation error occurred even after pre-deleting inactive records for IDs {user_discount_ids}: {e}",
                exc_info=True)
        else:
            logging.error(f"❌ Error deactivating user discounts for IDs {user_discount_ids}: {e}", exc_info=True)
        return 0



# ----------------- 🔹 إدارة المهام المجدولة ----------------- #

async def add_scheduled_task(connection, task_type: str, telegram_id: int, execute_at: datetime,
                             channel_id: Optional[int] = None, payload: Optional[dict[str, any]] = None,
                             clean_up: bool = True):
    try:
        if execute_at.tzinfo is None:
            execute_at = execute_at.replace(tzinfo=timezone.utc)
        else:
            execute_at = execute_at.astimezone(timezone.utc)

        if clean_up and channel_id:
            await connection.execute("""
                DELETE FROM scheduled_tasks
                WHERE telegram_id = $1 AND channel_id = $2 AND task_type = $3
            """, telegram_id, channel_id, task_type)

        # ⭐ التعديل الرئيسي هنا: تحويل القاموس إلى نص JSON ⭐
        # نستخدم (if payload else None) للتعامل مع حالة عدم وجود payload
        payload_json = json.dumps(payload) if payload else None

        await connection.execute("""
            INSERT INTO scheduled_tasks (task_type, telegram_id, channel_id, execute_at, status, payload)
            VALUES ($1, $2, $3, $4, 'pending', $5)
        """, task_type, telegram_id, channel_id, execute_at, payload_json) # <-- استخدام المتغير الجديد

        logging.info(f"✅ Scheduled task '{task_type}' for user {telegram_id} at {execute_at} with payload {payload}.")
        return True
    except Exception as e:
        # هنا سنحصل على تفاصيل الخطأ بشكل أفضل إذا استخدمنا exc_info=True
        logging.error(f"❌ Error adding scheduled task '{task_type}' for user {telegram_id}: {e}", exc_info=True)
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

async def get_failed_payment_for_retry(connection, payment_id: int):
    """
    Fetches a failed payment record with all necessary data for a renewal retry.
    """
    query = """
        SELECT 
            id, user_id, subscription_plan_id, amount, status, tx_hash, 
            telegram_id, payment_token, amount_received
        FROM payments
        WHERE id = $1 AND status = 'failed'
    """
    return await connection.fetchrow(query, payment_id)


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
    🔹 [مُعدل] جلب اشتراكات المستخدم الفعلية مع رابط الدعوة للقناة الرئيسية وقائمة بروابط القنوات الفرعية.
    """
    try:
        # 🌟 [الاستعلام المعدل] 🌟
        # نستخدم CTE (Common Table Expression) لتجميع روابط القنوات أولاً،
        # ثم نربطها بالاشتراكات للحصول على رابط القناة الرئيسية ومصفوفة JSON للقنوات الفرعية.
        subscriptions = await connection.fetch("""
            WITH ChannelData AS (
                SELECT
                    stc.subscription_type_id,
                    -- البحث عن رابط الدعوة للقناة الرئيسية
                    MAX(stc.invite_link) FILTER (WHERE stc.is_main = TRUE) as main_invite_link,
                    -- تجميع معلومات القنوات الفرعية في مصفوفة JSON
                    json_agg(
                        json_build_object(
                            'name', stc.channel_name,
                            'link', stc.invite_link
                        )
                    ) FILTER (WHERE stc.is_main = FALSE AND stc.invite_link IS NOT NULL) as sub_channel_links
                FROM
                    subscription_type_channels stc
                GROUP BY
                    stc.subscription_type_id
            )
            SELECT
                s.subscription_type_id,
                s.start_date,
                s.expiry_date,
                s.is_active,
                st.name AS subscription_name,
                -- جلب رابط الدعوة الرئيسي من CTE
                cd.main_invite_link,
                -- جلب روابط القنوات الفرعية من CTE
                cd.sub_channel_links
            FROM
                subscriptions s
            JOIN
                subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN
                ChannelData cd ON st.id = cd.subscription_type_id
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
            SET status = 'failed', error_message = $1, processed_at = NOW()
            WHERE payment_token = $2
            """,
            f"Subscription activation failed: {error_message}",
            payment_token
        )
        logging.warning(f"⚠️ تم تحديد الدفعة {payment_token} على أنها تحتاج لمراجعة يدوية.")
    except Exception as e:
        logging.error(f"❌ فشل تحديث حالة الدفع إلى 'failed' لـ {payment_token}: {e}")


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


async def link_user_gmail(connection, telegram_id: int, gmail: str) -> bool:
    """
    يقوم بربط أو تحديث البريد الإلكتروني (gmail) لمستخدم موجود بالفعل.

    Args:
        connection: اتصال قاعدة البيانات.
        telegram_id: معرف المستخدم في تليجرام.
        gmail: البريد الإلكتروني للمستخدم من تطبيق الموبايل.

    Returns:
        True إذا تم التحديث بنجاح, False في حالة حدوث خطأ.
    """
    try:
        # نحن نستخدم UPDATE فقط لأن المستخدم يجب أن يكون موجودًا بالفعل
        # من خلال تطبيقي المصغر قبل أن يتمكن من المزامنة.
        query = """
            UPDATE users SET gmail = $2 WHERE telegram_id = $1;
        """
        # execute ستعيد عدد الصفوف المتأثرة, يمكننا التحقق منها
        result = await connection.execute(query, telegram_id, gmail)

        # "UPDATE 1" يعني أنه تم تحديث صف واحد بنجاح
        if "UPDATE 1" in result:
            logging.info(f"✅ Gmail linked successfully for user {telegram_id}.")
            return True
        else:
            # هذا يعني أن الـ telegram_id لم يتم العثور عليه
            logging.warning(f"⚠️ Attempted to link gmail for non-existent user {telegram_id}.")
            return False

    except Exception as e:
        logging.error(f"❌ Error linking gmail for user {telegram_id}: {e}", exc_info=True)
        return False

