import os
import json
import logging
from quart import Blueprint, request, jsonify, abort, current_app, send_file
from config import DATABASE_CONFIG, SECRET_KEY
from auth import get_current_user
from datetime import datetime, timezone, timedelta
import pytz
from functools import wraps
import jwt
from utils.permissions import permission_required, owner_required, log_action
import asyncpg
import asyncio
from utils.notifications import create_notification
from utils.db_utils import remove_users_from_channel, generate_channel_invite_link, send_message_to_user, generate_shared_invite_link_for_channel
import io
import pandas as pd
from database.db_queries import (
    add_user,
    add_subscription,
    add_scheduled_task,
    cancel_subscription_db,
    delete_scheduled_tasks_for_subscription
)
from database.db_queries import update_subscription as update_subscription_db


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# إنشاء Blueprint مع بادئة URL
admin_routes = Blueprint("admin_routes", __name__, url_prefix="/api/admin")
LOCAL_TZ = pytz.timezone(os.getenv("LOCAL_TZ", "Asia/Riyadh"))
IS_DEVELOPMENT = os.getenv("FLASK_ENV", "production") == "development"


# --- دالة مساعدة لحساب التواريخ (مستحسنة) ---
async def _calculate_admin_subscription_dates(connection, telegram_id, main_channel_id, days_to_add, current_time_utc):
    """Helper function to calculate start and expiry dates for admin actions."""
    existing_main_channel_sub = await connection.fetchrow(
        "SELECT id, start_date, expiry_date, is_active FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
        telegram_id, main_channel_id
    )

    start_date_to_use = current_time_utc
    base_expiry_to_extend = current_time_utc

    if existing_main_channel_sub and \
            existing_main_channel_sub['is_active'] and \
            existing_main_channel_sub['expiry_date'] and \
            existing_main_channel_sub['expiry_date'] >= current_time_utc:
        start_date_to_use = existing_main_channel_sub['start_date']
        base_expiry_to_extend = existing_main_channel_sub['expiry_date']
    elif existing_main_channel_sub and existing_main_channel_sub['expiry_date'] and existing_main_channel_sub[
        'expiry_date'] < current_time_utc:
        # اشتراك سابق منتهي، نبدأ من الآن
        pass  # القيم الافتراضية صحيحة

    # إضافة دقائق للتطوير (اختياري، يمكنك إزالته إذا لم يكن مطلوباً لعمليات الإدارة)
    duration_minutes_dev = 120 if IS_DEVELOPMENT else 0
    new_expiry_date = base_expiry_to_extend + timedelta(days=days_to_add, minutes=duration_minutes_dev)

    return start_date_to_use, new_expiry_date


def role_required(required_role):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return jsonify({"error": "Authorization header missing"}), 401
            try:
                token = auth_header.split(" ")[1]  # Bearer <token>
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_role = payload.get("role")
                # يمكنك السماح للمالك أيضًا بالقيام بإجراءات الأدمن إذا رغبت
                if required_role == "admin" and user_role not in ["admin", "owner"]:
                    return jsonify({"error": "Admin privileges required"}), 403
                elif required_role == "owner" and user_role != "owner":
                    return jsonify({"error": "Owner privileges required"}), 403
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401

            return await func(*args, **kwargs)

        return wrapper

    return decorator


@admin_routes.route("/users_panel", methods=["GET"])
@permission_required("panel_users.read")
async def get_users_with_roles():
    """جلب قائمة المستخدمين مع أدوارهم من جدول panel_users و roles"""
    async with current_app.db_pool.acquire() as connection:
        users_data = await connection.fetch("""
            SELECT u.id, u.email, u.display_name, u.created_at, u.updated_at, r.name as role_name, r.id as role_id
            FROM panel_users u
            LEFT JOIN roles r ON u.role_id = r.id
            ORDER BY u.created_at DESC
        """)
        users_list = []
        for user_row in users_data:
            users_list.append({
                "id": user_row["id"],
                "email": user_row["email"],
                "display_name": user_row["display_name"],
                "role_name": user_row["role_name"],
                "role_id": user_row["role_id"],
                "created_at": user_row["created_at"].isoformat() if user_row["created_at"] else None,
                "updated_at": user_row["updated_at"].isoformat() if user_row["updated_at"] else None,
            })
    return jsonify({"users": users_list}), 200


@admin_routes.route("/users_panel", methods=["POST"])
@permission_required("panel_users.create")  # الصلاحية العامة لإنشاء المستخدمين
async def create_user_with_role():
    """إضافة مستخدم جديد مع تحديد دوره"""
    data = await request.get_json()
    email = data.get("email")
    display_name = data.get("display_name", "")
    role_id = data.get("role_id")  # الآن نستخدم role_id

    current_user_data = await get_current_user()  # لجلب معلومات المستخدم الحالي للتسجيل

    if not email or not role_id:
        return jsonify({"error": "Email and role_id are required"}), 400

    async with current_app.db_pool.acquire() as connection:
        async with connection.transaction():
            existing_user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)
            if existing_user:
                return jsonify({"error": "User already exists"}), 400

            # التحقق من وجود الدور
            role_exists = await connection.fetchrow("SELECT id, name FROM roles WHERE id = $1", role_id)
            if not role_exists:
                return jsonify({"error": "Role not found"}), 404

            # إذا كان المستخدم يحاول إنشاء "owner" وهو ليس "owner", يجب منعه
            # هذا يمكن معالجته أيضاً بواجهة المستخدم بعدم عرض "owner" كخيار إلا للـ owners
            # أو بإضافة decorator خاص @owner_can_create_owner_required
            if role_exists['name'] == 'owner':
                # تحقق إضافي: هل المستخدم الحالي هو owner؟
                current_user_role = await connection.fetchval("""
                    SELECT r.name FROM panel_users u
                    JOIN roles r ON u.role_id = r.id
                    WHERE u.email = $1
                """, current_user_data["email"])
                if current_user_role != 'owner':
                    await log_action(
                        current_user_data["email"],
                        "UNAUTHORIZED_CREATE_OWNER_ATTEMPT",
                        resource="user",
                        resource_id=email,
                        details={"target_role_id": role_id, "target_role_name": role_exists['name']}
                    )
                    return jsonify({"error": "Only owners can create other owners"}), 403

            new_user_id = await connection.fetchval(
                """INSERT INTO panel_users (email, display_name, role_id)
                   VALUES ($1, $2, $3) RETURNING id""",
                email, display_name, role_id
            )

            await log_action(
                current_user_data["email"],  # المستخدم الذي قام بالعملية
                "CREATE_USER",
                resource="user",
                resource_id=str(new_user_id),  # أو email
                details={"email": email, "display_name": display_name, "role_id": role_id,
                         "role_name": role_exists['name']}
            )

    return jsonify(
        {"message": f"User {email} added successfully with role {role_exists['name']}", "user_id": new_user_id}), 201


@admin_routes.route("/users/<int:user_id_to_delete>", methods=["DELETE"])  # استخدام user_id في المسار
@permission_required("panel_users.delete")
async def remove_user_by_id(user_id_to_delete: int):
    """حذف حساب مستخدم بناءً على ID"""
    current_user_data = await get_current_user()

    async with current_app.db_pool.acquire() as connection:
        async with connection.transaction():
            # التحقق مما إذا كان المستخدم موجودًا وجلب دوره
            existing_user = await connection.fetchrow("""
                SELECT u.id, u.email, r.name as role_name
                FROM panel_users u
                LEFT JOIN roles r ON u.role_id = r.id
                WHERE u.id = $1
            """, user_id_to_delete)

            if not existing_user:
                return jsonify({"error": "User not found"}), 404

            # التأكد من عدم حذف آخر Owner في النظام
            if existing_user["role_name"] == "owner":
                owners_count = await connection.fetchval("""
                    SELECT COUNT(*) FROM panel_users pu
                    JOIN roles r ON pu.role_id = r.id
                    WHERE r.name = 'owner'
                """)
                if owners_count <= 1:
                    await log_action(
                        current_user_data["email"],
                        "DELETE_LAST_OWNER_ATTEMPT",
                        resource="user",
                        resource_id=str(user_id_to_delete),
                        details={"deleted_user_email": existing_user["email"]}
                    )
                    return jsonify({"error": "Cannot delete the last owner"}), 403

            # التأكد من أن المستخدم لا يحذف نفسه (اختياري، لكنه جيد)
            if existing_user["email"] == current_user_data["email"]:
                await log_action(
                    current_user_data["email"],
                    "SELF_DELETE_USER_ATTEMPT",
                    resource="user",
                    resource_id=str(user_id_to_delete)
                )
                return jsonify({"error": "You cannot delete your own account this way."}), 403

            # تنفيذ الحذف
            await connection.execute("DELETE FROM panel_users WHERE id = $1", user_id_to_delete)

            await log_action(
                current_user_data["email"],
                "DELETE_USER",
                resource="user",
                resource_id=str(user_id_to_delete),  # أو email
                details={"deleted_user_email": existing_user["email"], "deleted_user_role": existing_user["role_name"]}
            )

    return jsonify(
        {"message": f"User with ID {user_id_to_delete} (Email: {existing_user['email']}) removed successfully"}), 200


# في ملف admin_routes.py
@admin_routes.route("/users", methods=["GET"])
@permission_required("bot_users.read")
async def get_users_endpoint():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        order_by_clause = "ORDER BY u.id DESC"

        base_query_select = """
            SELECT u.id, u.telegram_id, u.username, u.full_name, 
                   u.wallet_address, u.ton_wallet_address, u.wallet_app,
                   (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id) as subscription_count,
                   (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id AND s.is_active = true) as active_subscription_count
            FROM users u
        """
        count_base_query_select = "SELECT COUNT(u.id) as total FROM users u"

        where_clauses = ["1=1"]
        where_params = []

        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = [
                f"u.telegram_id::TEXT ILIKE ${len(where_params) + 1}",
                f"u.full_name ILIKE ${len(where_params) + 1}",
                f"u.username ILIKE ${len(where_params) + 1}"
            ]
            current_param_idx = len(where_params)
            search_conditions_sql = []

            search_conditions_sql.append(f"u.telegram_id::TEXT ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            current_param_idx += 1

            search_conditions_sql.append(f"u.full_name ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            current_param_idx += 1

            search_conditions_sql.append(f"u.username ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            # current_param_idx += 1 # لا حاجة للزيادة هنا

            where_clauses.append(f"({' OR '.join(search_conditions_sql)})")

        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0]

        query_final_params = list(where_params)
        query = f"""
            {base_query_select}
            WHERE {where_sql}
            {order_by_clause}
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.extend([page_size, offset])

        count_query = f"{count_base_query_select} WHERE {where_sql}"

        items_data = []
        total_records = 0

        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(query, *query_final_params)
            items_data = [dict(row) for row in rows]

            count_row = await conn.fetchrow(count_query, *where_params)
            total_records = count_row['total'] if count_row and count_row['total'] is not None else 0

        # الإحصائية هنا هي نفسها إجمالي عدد المستخدمين المطابقين للبحث
        users_stat_count = total_records

        return jsonify({
            "data": items_data,
            "total": total_records,  # تم التغيير من total_count
            "page": page,
            "page_size": page_size,
            "users_count": users_stat_count  # إحصائية بسيطة
        })

    except ValueError as ve:
        logging.error(f"Value error in /users: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters", "details": str(ve)}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /users: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed", "details": str(pe)}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /users: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@admin_routes.route("/users/<int:telegram_id>", methods=["GET"])
@permission_required("bot_users.read_details")
async def get_user_details(telegram_id):
    try:
        async with current_app.db_pool.acquire() as conn:
            # جلب بيانات المستخدم الأساسية
            user_query = """
                SELECT u.id, u.telegram_id, u.username, u.full_name, u.wallet_address, 
                       u.ton_wallet_address, u.wallet_app
                FROM users u
                WHERE u.telegram_id = $1
            """
            user_data = await conn.fetchrow(user_query, telegram_id)

            if not user_data:
                return jsonify({"error": "User not found"}), 404

            user_result = dict(user_data)

            # جلب الاشتراكات
            subscriptions_query = """
                SELECT s.id, s.channel_id, s.expiry_date, s.is_active, s.start_date,
                       s.subscription_type_id, st.name as subscription_type_name
                FROM subscriptions s
                LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
                WHERE s.telegram_id = $1
                ORDER BY s.expiry_date DESC
            """
            subscriptions_data = await conn.fetch(subscriptions_query, telegram_id)
            user_result["subscriptions"] = [dict(row) for row in subscriptions_data]

            # حساب إجمالي المدفوعات
            payments_query = """
                SELECT COALESCE(SUM(amount_received), 0) as total_payments,
                       COUNT(*) as payment_count
                FROM payments
                WHERE telegram_id = $1 AND status = 'completed' AND currency = 'USDT'
            """
            payment_data = await conn.fetchrow(payments_query, telegram_id)

            if payment_data:
                user_result["payment_stats"] = {
                    "total_payments": float(payment_data["total_payments"]) if payment_data["total_payments"] else 0,
                    "payment_count": payment_data["payment_count"]
                }
            else:
                user_result["payment_stats"] = {
                    "total_payments": 0,
                    "payment_count": 0
                }

            # جلب آخر المدفوعات
            recent_payments_query = """
                SELECT id, amount, created_at, status, payment_method, payment_token, currency
                FROM payments
                WHERE telegram_id = $1
                AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT 5
            """
            recent_payments = await conn.fetch(recent_payments_query, telegram_id)
            user_result["recent_payments"] = [dict(row) for row in recent_payments]

            return jsonify(user_result)

    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /users/{telegram_id}: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed"}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /users/{telegram_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


#######################################
# نقاط API لإدارة أنواع الاشتراكات (subscription_types)
#######################################


# --- إنشاء نوع اشتراك جديد ---
@admin_routes.route("/subscription-types", methods=["POST"])
@permission_required("subscription_types.create")
async def create_subscription_type():
    try:
        data = await request.get_json()
        name = data.get("name")
        main_channel_id_str = data.get("main_channel_id")
        description = data.get("description", "")
        image_url = data.get("image_url", "")
        features = data.get("features", [])
        terms_and_conditions = data.get("terms_and_conditions", [])  # <-- إضافة جديدة
        usp = data.get("usp", "")
        is_active = data.get("is_active", True)
        secondary_channels_data = data.get("secondary_channels", [])
        main_channel_name_from_data = data.get("main_channel_name", f"Main Channel for {name}")

        if not name or main_channel_id_str is None:
            return jsonify({"error": "Missing required fields: name and main_channel_id"}), 400

        try:
            main_channel_id = int(main_channel_id_str)
        except ValueError:
            return jsonify({"error": "main_channel_id must be an integer"}), 400

        # ... (التحقق من صحة secondary_channels_data كما هو موجود) ...
        valid_secondary_channels = []
        if not isinstance(secondary_channels_data, list):
            return jsonify({"error": "secondary_channels must be a list"}), 400

        for ch_data in secondary_channels_data:
            if not isinstance(ch_data, dict) or "channel_id" not in ch_data:
                return jsonify({"error": "Each secondary channel must be an object with a 'channel_id'"}), 400
            try:
                ch_id = int(ch_data["channel_id"])
                ch_name = ch_data.get("channel_name")
                if ch_id == main_channel_id:
                    return jsonify(
                        {"error": f"Secondary channel ID {ch_id} cannot be the same as the main channel ID."}), 400
                valid_secondary_channels.append({"channel_id": ch_id, "channel_name": ch_name})
            except ValueError:
                return jsonify({
                    "error": f"Invalid channel_id '{ch_data['channel_id']}' in secondary_channels. Must be an integer."}), 400

        if not isinstance(features, list):
            return jsonify({"error": "features must be a list of strings"}), 400
        if not isinstance(terms_and_conditions, list):  # <-- تحقق جديد
            return jsonify({"error": "terms_and_conditions must be a list of strings"}), 400

        async with current_app.db_pool.acquire() as connection:
            async with connection.transaction():
                query_type = """
                    INSERT INTO subscription_types
                    (name, channel_id, description, image_url, features, usp, is_active, terms_and_conditions)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb)
                    RETURNING id, name, channel_id AS main_channel_id, description, image_url, 
                              features, usp, is_active, created_at, terms_and_conditions;
                """
                created_type = await connection.fetchrow(
                    query_type, name, main_channel_id, description, image_url,
                    json.dumps(features), usp, is_active, json.dumps(terms_and_conditions)  # <-- إضافة جديدة
                )
                if not created_type:
                    raise Exception("Failed to create subscription type record.")

                new_type_id = created_type["id"]

                # ... (إدراج القنوات الرئيسية والفرعية كما هو موجود) ...
                await connection.execute(
                    """
                    INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                    channel_name = EXCLUDED.channel_name, is_main = TRUE;
                    """,
                    new_type_id, main_channel_id, main_channel_name_from_data
                )

                if valid_secondary_channels:
                    for sec_channel in valid_secondary_channels:
                        await connection.execute(
                            """
                            INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                            VALUES ($1, $2, $3, FALSE)
                            ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                            channel_name = EXCLUDED.channel_name, is_main = FALSE;
                            """,
                            new_type_id, sec_channel["channel_id"], sec_channel.get("channel_name")
                        )

                linked_channels_query = "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1"
                linked_channels_rows = await connection.fetch(linked_channels_query, new_type_id)

                response_data = dict(created_type)
                # التأكد من أن features و terms_and_conditions هي قائمة في الاستجابة
                if isinstance(response_data.get("features"), str):
                    response_data["features"] = json.loads(response_data["features"])
                if isinstance(response_data.get("terms_and_conditions"), str):  # <-- إضافة جديدة
                    response_data["terms_and_conditions"] = json.loads(response_data["terms_and_conditions"])

                response_data["linked_channels"] = [dict(row) for row in linked_channels_rows]

        return jsonify(response_data), 201

    except Exception as e:
        logging.error("Error creating subscription type: %s", e, exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}),


# --- تعديل بيانات نوع اشتراك موجود ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["PUT"])
@permission_required("subscription_types.update")
async def update_subscription_type(type_id: int):
    try:
        data = await request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        name = data.get("name")
        new_main_channel_id_input = data.get("main_channel_id")
        main_channel_name_input = data.get("main_channel_name")
        secondary_channels_data = data.get("secondary_channels")
        send_invites_for_new_channels = data.get("send_invites_for_new_channels", False)
        description = data.get("description")
        image_url = data.get("image_url")
        features = data.get("features")
        usp = data.get("usp")
        is_active = data.get("is_active")
        terms_and_conditions = data.get("terms_and_conditions")

        new_main_channel_id = None
        if new_main_channel_id_input is not None:
            try:
                temp_main_id_str = str(new_main_channel_id_input).strip()
                if not temp_main_id_str:
                    return jsonify({"error": "main_channel_id cannot be an empty string if provided"}), 400
                new_main_channel_id = int(temp_main_id_str)
            except ValueError:
                return jsonify({
                                   "error": f"main_channel_id '{new_main_channel_id_input}' must be a valid integer if provided"}), 400

        valid_new_secondary_channels = []
        if secondary_channels_data is not None:
            if not isinstance(secondary_channels_data, list):
                return jsonify({"error": "secondary_channels must be a list if provided"}), 400

            for i, ch_data in enumerate(secondary_channels_data):
                if not isinstance(ch_data, dict) or "channel_id" not in ch_data:
                    return jsonify(
                        {"error": f"Each secondary channel (index {i}) must be an object with 'channel_id'"}), 400
                ch_id_value = ch_data.get("channel_id")
                if ch_id_value is None:
                    logging.warning(
                        f"Secondary channel data at index {i} is missing 'channel_id' or it's null: {ch_data}")
                    continue
                ch_id_as_str = str(ch_id_value).strip()
                if not ch_id_as_str:
                    logging.warning(
                        f"Secondary channel data at index {i} has an empty 'channel_id' after stripping: {ch_data}")
                    continue
                try:
                    ch_id = int(ch_id_as_str)
                    ch_name_value = ch_data.get("channel_name", f"Secondary Channel {ch_id}")
                    ch_name_processed = str(ch_name_value).strip() or f"Secondary Channel {ch_id}"
                    valid_new_secondary_channels.append({
                        "channel_id": ch_id,
                        "channel_name": ch_name_processed
                    })
                except ValueError:
                    return jsonify(
                        {
                            "error": f"Invalid channel_id format '{ch_id_value}' in secondary_channels (index {i}). Must be a numeric string or number."}), 400

        updated_type = None  # سيتم تعيينه داخل الـ transaction
        newly_added_secondary_channels_for_actions = []  # سيتم تعيينه داخل الـ transaction
        effective_main_channel_id_after_update = None  # سيتم تعيينه داخل الـ transaction

        async with current_app.db_pool.acquire() as connection:
            async with connection.transaction():
                current_main_channel_id_db = new_main_channel_id
                if current_main_channel_id_db is None:
                    current_main_channel_id_db = await connection.fetchval(
                        "SELECT channel_id FROM subscription_types WHERE id = $1", type_id)
                    if current_main_channel_id_db is None:
                        return jsonify({"error": "Subscription type not found"}), 404

                for sec_ch in valid_new_secondary_channels:
                    if sec_ch["channel_id"] == current_main_channel_id_db:
                        return jsonify({
                            "error": f"Secondary channel ID {sec_ch['channel_id']} conflicts with the effective main channel ID {current_main_channel_id_db}."}), 400

                current_secondary_channels_rows = await connection.fetch(
                    "SELECT channel_id FROM subscription_type_channels WHERE subscription_type_id = $1 AND is_main = FALSE",
                    type_id
                )
                current_secondary_channel_ids = {row['channel_id'] for row in current_secondary_channels_rows}

                query_type_update = """
                    UPDATE subscription_types
                    SET name = COALESCE($1, name),
                        channel_id = COALESCE($2, channel_id), 
                        description = COALESCE($3, description),
                        image_url = COALESCE($4, image_url),
                        features = COALESCE($5::jsonb, features),
                        usp = COALESCE($6, usp),
                        is_active = COALESCE($7, is_active),
                        terms_and_conditions = COALESCE($8::jsonb, terms_and_conditions)
                    WHERE id = $9
                    RETURNING id, name, channel_id AS main_channel_id, description, image_url, 
                              features, usp, is_active, created_at, terms_and_conditions,
                              (SELECT channel_name FROM subscription_type_channels stc WHERE stc.subscription_type_id = $9 AND stc.channel_id = subscription_types.channel_id AND stc.is_main = TRUE LIMIT 1) as current_main_channel_name;
                """
                features_json = json.dumps(features) if features is not None else None
                terms_json = json.dumps(terms_and_conditions) if terms_and_conditions is not None else None

                updated_type_row = await connection.fetchrow(  # تم تغيير اسم المتغير لتجنب الالتباس
                    query_type_update, name, new_main_channel_id, description,
                    image_url, features_json, usp, is_active,
                    terms_json, type_id
                )

                if not updated_type_row:
                    check_exists = await connection.fetchval("SELECT id FROM subscription_types WHERE id = $1", type_id)
                    if not check_exists:
                        return jsonify({"error": "Subscription type not found"}), 404
                    logging.warning(
                        f"Subscription type {type_id} found but not updated. This might indicate no actual changes were made or an issue with COALESCE.")
                    updated_type_row = await connection.fetchrow(
                        """SELECT id, name, channel_id AS main_channel_id, description, image_url, 
                                  features, usp, is_active, created_at, terms_and_conditions,
                                  (SELECT channel_name FROM subscription_type_channels stc WHERE stc.subscription_type_id = $1 AND stc.channel_id = subscription_types.channel_id AND stc.is_main = TRUE LIMIT 1) as current_main_channel_name
                           FROM subscription_types WHERE id = $1""", type_id
                    )
                    if not updated_type_row:
                        return jsonify({"error": "Subscription type not found after attempting update."}), 404

                updated_type = dict(updated_type_row)  # تحويل إلى dict لاستخدامه لاحقًا
                effective_main_channel_id_after_update = updated_type["main_channel_id"]

                main_channel_name_to_use = ""
                if main_channel_name_input is not None:
                    main_channel_name_to_use = str(main_channel_name_input).strip()

                if not main_channel_name_to_use:
                    existing_main_ch_name_from_db = updated_type.get("current_main_channel_name")
                    if existing_main_ch_name_from_db and str(existing_main_ch_name_from_db).strip():
                        main_channel_name_to_use = str(existing_main_ch_name_from_db).strip()
                    else:
                        main_channel_name_to_use = f"Main Channel for {updated_type['name']}"

                if effective_main_channel_id_after_update is not None:
                    await connection.execute(
                        "UPDATE subscription_type_channels SET is_main = FALSE WHERE subscription_type_id = $1 AND channel_id != $2",
                        type_id, effective_main_channel_id_after_update
                    )
                else:
                    await connection.execute(
                        "UPDATE subscription_type_channels SET is_main = FALSE WHERE subscription_type_id = $1",
                        type_id
                    )

                if effective_main_channel_id_after_update is not None:
                    await connection.execute(
                        """
                        INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                        VALUES ($1, $2, $3, TRUE)
                        ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                        channel_name = EXCLUDED.channel_name, is_main = TRUE;
                        """,
                        type_id, effective_main_channel_id_after_update, main_channel_name_to_use
                    )

                # newly_added_secondary_channels_for_actions تم تعريفه في النطاق الخارجي
                if secondary_channels_data is not None:
                    ids_in_new_secondary_list = {ch['channel_id'] for ch in valid_new_secondary_channels}
                    delete_query = "DELETE FROM subscription_type_channels WHERE subscription_type_id = $1 AND is_main = FALSE"
                    params = [type_id]
                    if ids_in_new_secondary_list:
                        delete_query += " AND channel_id NOT IN (SELECT unnest($2::bigint[]))"
                        params.append(list(ids_in_new_secondary_list))
                    await connection.execute(delete_query, *params)

                    for sec_channel_data in valid_new_secondary_channels:
                        ch_id = sec_channel_data["channel_id"]
                        ch_name = sec_channel_data["channel_name"]
                        if ch_id == effective_main_channel_id_after_update:
                            logging.warning(
                                f"Attempted to add main channel {ch_id} as secondary. Skipping for subscription_type {type_id}.")
                            continue
                        await connection.execute(
                            """
                            INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                            VALUES ($1, $2, $3, FALSE)
                            ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                            channel_name = EXCLUDED.channel_name, is_main = FALSE; 
                            """,
                            type_id, ch_id, ch_name
                        )
                        if ch_id not in current_secondary_channel_ids:
                            # هذه هي القنوات الفرعية الجديدة حقًا
                            newly_added_secondary_channels_for_actions.append(sec_channel_data)

                linked_channels_rows = await connection.fetch(
                    "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC, channel_name",
                    type_id
                )

                # تحديث updated_type بالبيانات الفعلية قبل إرسالها كاستجابة
                if isinstance(updated_type.get("features"), str):
                    updated_type["features"] = json.loads(updated_type["features"])
                if isinstance(updated_type.get("terms_and_conditions"), str):
                    updated_type["terms_and_conditions"] = json.loads(updated_type["terms_and_conditions"])
                updated_type["linked_channels"] = [dict(row) for row in linked_channels_rows]
            # --- نهاية Transaction ---

            # --------------------------------------------------------------------------------
            # --- جدولة المهام وإرسال الدعوات للقنوات الفرعية الجديدة ---
            # --------------------------------------------------------------------------------
            if newly_added_secondary_channels_for_actions:
                logging.info(
                    f"Processing {len(newly_added_secondary_channels_for_actions)} new secondary channels for type '{updated_type['name']}' (ID: {type_id})")

                # جلب المشتركين النشطين (كما هو)
                active_subscribers_for_actions = await connection.fetch(
                    """
                    SELECT s.telegram_id, s.expiry_date, u.full_name, u.username 
                    FROM subscriptions s
                    LEFT JOIN users u ON s.telegram_id = u.telegram_id
                    WHERE s.subscription_type_id = $1
                      AND s.is_active = TRUE
                      AND s.expiry_date > NOW()
                    GROUP BY s.telegram_id, s.expiry_date, u.full_name, u.username; 
                    """,
                    type_id
                )

                # <<< الخطوة الجديدة: إنشاء روابط الدعوة المشتركة للقنوات الجديدة >>>
                shared_invite_links_map = {}  # {channel_id: "invite_link_str"}
                if send_invites_for_new_channels and newly_added_secondary_channels_for_actions:
                    logging.info(
                        f"Generating shared invite links for {len(newly_added_secondary_channels_for_actions)} new channels.")
                    for new_channel_data in newly_added_secondary_channels_for_actions:
                        new_channel_id = new_channel_data['channel_id']
                        new_channel_name = new_channel_data['channel_name']

                        if new_channel_id == effective_main_channel_id_after_update:  # تجنب الرئيسية
                            continue

                        # اسم وصفي للرابط يمكن رؤيته في إعدادات القناة
                        link_name_prefix = f"دعوة لـ {updated_type['name']}"

                        invite_result = await generate_shared_invite_link_for_channel(
                            channel_id=new_channel_id,
                            channel_name=new_channel_name,
                            link_name_prefix=link_name_prefix
                        )
                        if invite_result and invite_result.get("success"):
                            shared_invite_links_map[new_channel_id] = invite_result.get("invite_link")
                        else:
                            logging.error(
                                f"Failed to generate shared invite link for channel '{new_channel_name}' ({new_channel_id}). Error: {invite_result.get('error')}")
                            # يمكنك هنا أن تقرر ما إذا كنت تريد إيقاف العملية أو المتابعة بدون هذا الرابط

                if active_subscribers_for_actions:
                    logging.info(
                        f"Found {len(active_subscribers_for_actions)} active subscribers for type {type_id} for post-update actions.")

                    for subscriber_idx, subscriber in enumerate(active_subscribers_for_actions):
                        subscriber_telegram_id = subscriber['telegram_id']
                        subscriber_expiry_date = subscriber['expiry_date']

                        # 1. جدولة مهام 'remove_user' (كما هو)
                        for new_channel_data in newly_added_secondary_channels_for_actions:
                            new_channel_id = new_channel_data['channel_id']
                            if new_channel_id == effective_main_channel_id_after_update:
                                continue
                            await add_scheduled_task(
                                connection=connection,
                                task_type='remove_user',
                                telegram_id=subscriber_telegram_id,
                                channel_id=new_channel_id,
                                execute_at=subscriber_expiry_date,
                                clean_up=True
                            )

                        # 2. إرسال روابط الدعوة إذا كان الخيار مفعلًا وكانت هناك روابط تم إنشاؤها
                        if send_invites_for_new_channels and shared_invite_links_map:
                            full_name = subscriber.get('full_name')
                            username = subscriber.get('username')
                            user_identifier = full_name or (f"@{username}" if username else str(subscriber_telegram_id))

                            channel_links_to_send_in_message = []
                            for new_channel_data in newly_added_secondary_channels_for_actions:
                                new_channel_id = new_channel_data['channel_id']
                                new_channel_name = new_channel_data['channel_name']

                                if new_channel_id == effective_main_channel_id_after_update:
                                    continue

                                # استخدم الرابط المشترك الذي تم إنشاؤه مسبقًا
                                invite_link_str = shared_invite_links_map.get(new_channel_id)
                                if invite_link_str:
                                    channel_links_to_send_in_message.append(
                                        f"▫️ قناة <a href='{invite_link_str}'>{new_channel_name}</a>"
                                    )
                                # لا داعي للـ else هنا، فقد تم تسجيل الخطأ عند محاولة إنشاء الرابط المشترك

                            if channel_links_to_send_in_message:
                                message_text = (
                                        f"📬 مرحبًا {user_identifier},\n\n"
                                        f"تمت إضافة قنوات جديدة إلى اشتراكك في \"<b>{updated_type['name']}</b>\":\n\n" +
                                        "\n".join(channel_links_to_send_in_message) +
                                        "\n\n💡 هذه الروابط صالحة للاستخدام للانضمام. يرجى الانضمام في أقرب وقت."
                                )
                                try:
                                    # أضف تأخير بسيط بين إرسال الرسائل للمستخدمين إذا كان عددهم كبيراً جداً
                                    # لتجنب مشاكل flood مع send_message أيضاً (أقل شيوعاً ولكن ممكنة)
                                    if subscriber_idx > 0 and subscriber_idx % 20 == 0:  # كل 20 رسالة
                                        logging.info(
                                            f"Pausing for 1 second before sending more messages (sent {subscriber_idx})")
                                        await asyncio.sleep(1)

                                    sent_successfully = await send_message_to_user(subscriber_telegram_id, message_text)
                                    if sent_successfully:
                                        logging.info(
                                            f"Sent aggregated invite links message to user {subscriber_telegram_id} for type {type_id}")
                                    else:
                                        logging.warning(
                                            f"Failed to send aggregated invite links message to user {subscriber_telegram_id} for type {type_id}")
                                except Exception as e_send:
                                    logging.error(
                                        f"Exception in send_message_to_user for {subscriber_telegram_id}: {e_send}")
                            else:
                                logging.info(
                                    f"No new channel links were available/generated to send to user {subscriber_telegram_id} for type {type_id}.")
                else:
                    logging.info(f"No active subscribers found for type {type_id} to schedule tasks or send invites.")

            elif send_invites_for_new_channels and not newly_added_secondary_channels_for_actions:
                logging.info(
                    f"Send invites was checked, but no genuinely new secondary channels were added for type {type_id}, so no tasks scheduled or invites sent.")

            return jsonify(updated_type), 200  # updated_type يحتوي الآن على linked_channels

    except ValueError as ve:
        logging.error(f"ValueError in update_subscription_type for type_id {type_id}: {ve}", exc_info=True)
        return jsonify({"error": f"Invalid data format: {str(ve)}"}), 400
    except Exception as e:
        logging.error(f"Error updating subscription type {type_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error while updating subscription type {type_id}"}), 500

# --- حذف نوع اشتراك ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["DELETE"])
@permission_required("subscription_types.delete")
async def delete_subscription_type(type_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            # الحذف من subscription_types سيؤدي إلى حذف السجلات المرتبطة
            # من subscription_type_channels بسبب ON DELETE CASCADE
            # تأكد من أن هذا هو السلوك المطلوب.
            # إذا كان هناك اشتراكات قائمة subscriptions أو خطط subscription_plans مرتبطة بهذا النوع،
            # قد تحتاج إلى التعامل معها (منع الحذف أو حذفها أيضًا إذا كان ذلك مناسبًا).
            # حاليًا، subscription_plans لديها ON DELETE CASCADE.
            # subscriptions ليس لديها، لذا قد يحدث خطأ إذا كان هناك اشتراكات مرتبطة.
            # يجب إما إضافة ON DELETE CASCADE أو SET NULL لـ subscriptions.subscription_type_id
            # أو التحقق برمجيًا هنا.

            # تحقق مبدئي إذا كان هناك اشتراكات مرتبطة (اختياري، لكن جيد)
            active_subs_count = await connection.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE subscription_type_id = $1", type_id)
            if active_subs_count > 0:
                return jsonify({
                    "error": f"Cannot delete. There are {active_subs_count} active subscriptions linked to this type."}), 409  # Conflict

            await connection.execute("DELETE FROM subscription_types WHERE id = $1", type_id)
        return jsonify({"message": "Subscription type and its associated channels deleted successfully"}), 200
    except Exception as e:
        logging.error("Error deleting subscription type %s: %s", type_id, e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# --- جلب قائمة أنواع الاشتراكات ---
@admin_routes.route("/subscription-types", methods=["GET"])
@permission_required("subscription_types.read")
async def get_subscription_types():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    st.id, st.name, st.channel_id AS main_channel_id, st.description, 
                    st.image_url, st.features, st.usp, st.is_active, st.created_at,
                    st.terms_and_conditions, -- <-- إضافة جديدة
                    (SELECT json_agg(json_build_object('channel_id', stc.channel_id, 'channel_name', stc.channel_name, 'is_main', stc.is_main)) 
                     FROM subscription_type_channels stc 
                     WHERE stc.subscription_type_id = st.id) AS linked_channels
                FROM subscription_types st
                ORDER BY st.created_at DESC;
            """
            results = await connection.fetch(query)

        types_list = []
        for row_data in results:
            type_item = dict(row_data)
            # features و terms_and_conditions يفترض أن تُرجع كـ list/dict من asyncpg/psycopg
            # إذا كانت تُرجع كنص JSON، عندها ستحتاج للتحويل.
            # asyncpg عادة ما يحول jsonb إلى Python dict/list تلقائيًا
            if isinstance(type_item.get("features"), str):  # احتياطًا
                type_item["features"] = json.loads(type_item["features"]) if type_item["features"] else []
            elif type_item.get("features") is None:
                type_item["features"] = []

            if isinstance(type_item.get("terms_and_conditions"), str):  # <-- إضافة جديدة, احتياطًا
                type_item["terms_and_conditions"] = json.loads(type_item["terms_and_conditions"]) if type_item[
                    "terms_and_conditions"] else []
            elif type_item.get("terms_and_conditions") is None:  # <-- إضافة جديدة
                type_item["terms_and_conditions"] = []

            if isinstance(type_item.get("linked_channels"), str):
                type_item["linked_channels"] = json.loads(type_item["linked_channels"]) if type_item[
                    "linked_channels"] else []
            elif type_item.get("linked_channels") is None:
                type_item["linked_channels"] = []

            types_list.append(type_item)

        return jsonify(types_list), 200
    except Exception as e:
        logging.error("Error fetching subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# --- جلب تفاصيل نوع اشتراك معين ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["GET"])
@permission_required("subscription_types.read")
async def get_subscription_type(type_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            query_type = """
                SELECT id, name, channel_id AS main_channel_id, description, image_url, 
                       features, usp, is_active, created_at, terms_and_conditions -- <-- إضافة جديدة
                FROM subscription_types
                WHERE id = $1;
            """
            type_details = await connection.fetchrow(query_type, type_id)

            if not type_details:
                return jsonify({"error": "Subscription type not found"}), 404

            query_channels = """
                SELECT channel_id, channel_name, is_main
                FROM subscription_type_channels
                WHERE subscription_type_id = $1
                ORDER BY is_main DESC, channel_name;
            """
            linked_channels_rows = await connection.fetch(query_channels, type_id)

            response_data = dict(type_details)
            # asyncpg عادة ما يحول jsonb إلى Python dict/list تلقائيًا
            if isinstance(response_data.get("features"), str):  # احتياطًا
                response_data["features"] = json.loads(response_data["features"]) if response_data["features"] else []
            elif response_data.get("features") is None:
                response_data["features"] = []

            if isinstance(response_data.get("terms_and_conditions"), str):  # <-- إضافة جديدة, احتياطًا
                response_data["terms_and_conditions"] = json.loads(response_data["terms_and_conditions"]) if \
                response_data["terms_and_conditions"] else []
            elif response_data.get("terms_and_conditions") is None:  # <-- إضافة جديدة
                response_data["terms_and_conditions"] = []

            response_data["linked_channels"] = [dict(row) for row in linked_channels_rows]

        return jsonify(response_data), 200
    except Exception as e:
        logging.error("Error fetching subscription type %s: %s", type_id, e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


#######################################
# نقاط API لإدارة خطط الاشتراك (subscription_plans)
#######################################

@admin_routes.route("/subscription-plans", methods=["POST"])
@permission_required("subscription_plans.create")
async def create_subscription_plan():
    try:
        data = await request.get_json()
        subscription_type_id = data.get("subscription_type_id")
        name = data.get("name")
        price = data.get("price")
        original_price = data.get("original_price", price)  # إذا لم يتم توفير سعر أصلي، استخدم السعر العادي
        duration_days = data.get("duration_days")
        telegram_stars_price = data.get("telegram_stars_price", 0)
        is_active = data.get("is_active", True)

        if not subscription_type_id or not name or price is None or duration_days is None:
            return jsonify({"error": "Missing required fields"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                INSERT INTO subscription_plans
                (subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active, created_at;
            """
            result = await connection.fetchrow(
                query,
                subscription_type_id,
                name,
                price,
                original_price,
                telegram_stars_price,
                duration_days,
                is_active
            )
        return jsonify(dict(result)), 201
    except Exception as e:
        logging.error("Error creating subscription plan: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# تعديل بيانات خطة اشتراك
@admin_routes.route("/subscription-plans/<int:plan_id>", methods=["PUT"])
@permission_required("subscription_plans.update")
async def update_subscription_plan(plan_id: int):
    try:
        data = await request.get_json()
        subscription_type_id = data.get("subscription_type_id")
        name = data.get("name")
        price = data.get("price")
        original_price = data.get("original_price")
        duration_days = data.get("duration_days")
        telegram_stars_price = data.get("telegram_stars_price")
        is_active = data.get("is_active")

        # إذا تم تعديل السعر ولم يتم تحديد سعر أصلي، اجعل السعر الأصلي هو نفس السعر الجديد
        if price is not None and original_price is None:
            async with current_app.db_pool.acquire() as connection:
                # الحصول على السعر الأصلي الحالي
                current_plan = await connection.fetchrow(
                    "SELECT price, original_price FROM subscription_plans WHERE id = $1", plan_id
                )

                # التحقق مما إذا كان السعر الأصلي الحالي مساويًا للسعر الحالي
                if current_plan and current_plan['price'] == current_plan['original_price']:
                    original_price = price

        async with current_app.db_pool.acquire() as connection:
            query = """
                UPDATE subscription_plans
                SET subscription_type_id = COALESCE($1, subscription_type_id),
                    name = COALESCE($2, name),
                    price = COALESCE($3, price),
                    original_price = COALESCE($4, original_price),
                    telegram_stars_price = COALESCE($5, telegram_stars_price),
                    duration_days = COALESCE($6, duration_days),
                    is_active = COALESCE($7, is_active)
                WHERE id = $8
                RETURNING id, subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active, created_at;
            """
            result = await connection.fetchrow(
                query,
                subscription_type_id,
                name,
                price,
                original_price,
                telegram_stars_price,
                duration_days,
                is_active,
                plan_id
            )
        if result:
            return jsonify(dict(result)), 200
        else:
            return jsonify({"error": "Subscription plan not found"}), 404
    except Exception as e:
        logging.error("Error updating subscription plan: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/subscription-plans/<int:plan_id>", methods=["DELETE"])
@permission_required("subscription_plans.update")  # تأكد من أن هذا الصلاحية صحيحة
async def delete_subscription_plan(plan_id: int):  # تم تغيير اسم الدالة للمفرد
    try:
        async with current_app.db_pool.acquire() as connection:
            # اختياري: التحقق مما إذا كانت الخطة موجودة أولاً وما إذا تم حذف أي شيء
            # result = await connection.fetchrow("DELETE FROM subscription_plans WHERE id = $1 RETURNING id", plan_id)
            # if not result:
            #     return jsonify({"error": "Subscription plan not found"}), 404

            # الحذف المباشر
            await connection.execute("DELETE FROM subscription_plans WHERE id = $1", plan_id)

        # رسالة نجاح صحيحة
        return jsonify({"message": "Subscription plan deleted successfully"}), 200
    except Exception as e:
        # رسالة خطأ تسجيل صحيحة
        logging.error("Error deleting subscription plan %s: %s", plan_id, e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# جلب جميع خطط الاشتراك، مع إمكانية التصفية حسب subscription_type_id
@admin_routes.route("/subscription-plans", methods=["GET"])
@permission_required("subscription_plans.read")
async def get_subscription_plans():
    try:
        subscription_type_id = request.args.get("subscription_type_id")
        async with current_app.db_pool.acquire() as connection:
            if subscription_type_id:
                query = """
                    SELECT id, subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active, created_at
                    FROM subscription_plans
                    WHERE subscription_type_id = $1
                    ORDER BY created_at DESC
                """
                results = await connection.fetch(query, int(subscription_type_id))
            else:
                query = """
                    SELECT id, subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active, created_at
                    FROM subscription_plans
                    ORDER BY created_at DESC
                """
                results = await connection.fetch(query)
        plans = [dict(r) for r in results]
        return jsonify(plans), 200
    except Exception as e:
        logging.error("Error fetching subscription plans: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# جلب تفاصيل خطة اشتراك معينة
@admin_routes.route("/subscription-plans/<int:plan_id>", methods=["GET"])
@permission_required("subscription_plans.read")
async def get_subscription_plan(plan_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, subscription_type_id, name, price, original_price, telegram_stars_price, duration_days, is_active, created_at
                FROM subscription_plans
                WHERE id = $1
            """
            result = await connection.fetchrow(query, plan_id)
        if result:
            return jsonify(dict(result)), 200
        else:
            return jsonify({"error": "Subscription plan not found"}), 404
    except Exception as e:
        logging.error("Error fetching subscription plan: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/subscriptions", methods=["GET"])
@permission_required("user_subscriptions.read")
async def get_subscriptions_endpoint():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        # الفلاتر من الواجهة الأمامية الجديدة
        status_filter = request.args.get("status")  # "active", "inactive", "all"
        type_filter_id_str = request.args.get("subscription_type_id")  # سيكون ID أو "all"
        source_filter = request.args.get("source")  # سيكون قيمة المصدر أو "all"
        start_date_filter = request.args.get("start_date")  # لـ expiry_date (YYYY-MM-DD)
        end_date_filter = request.args.get("end_date")  # لـ expiry_date (YYYY-MM-DD)

        # الفرز: للتوحيد مع PaymentsPage، سنقوم بالفرز الافتراضي حاليًا.
        # إذا أردت دعم الفرز من جانب الخادم لاحقًا، ستحتاج لتمرير sortModel من DataTable.
        order_by_clause = "ORDER BY s.id DESC"  # أو s.created_at DESC إذا كان لديك ومناسبًا

        base_query_select = """
            SELECT
                s.id, s.user_id, s.expiry_date, s.is_active, s.channel_id, 
                s.subscription_type_id, s.telegram_id, s.start_date, 
                s.updated_at, s.payment_id, s.subscription_plan_id, s.source,
                u.full_name, u.username, 
                st.name AS subscription_type_name,
                sp.name AS subscription_plan_name 
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
        """

        count_base_query_select = """
            SELECT COUNT(s.id) as total
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
        """

        where_clauses = ["1=1"]
        where_params = []

        # 1. فلتر الحالة (is_active)
        if status_filter and status_filter.lower() != "all":
            is_active_bool = status_filter.lower() == "active"  # أو true إذا كانت الواجهة ترسلها
            where_clauses.append(f"s.is_active = ${len(where_params) + 1}")
            where_params.append(is_active_bool)

        # 2. فلتر نوع الاشتراك (subscription_type_id)
        if type_filter_id_str and type_filter_id_str.strip().lower() != "all":
            try:
                type_id_val = int(type_filter_id_str)
                where_clauses.append(f"s.subscription_type_id = ${len(where_params) + 1}")
                where_params.append(type_id_val)
            except ValueError:
                logging.warning(f"Invalid subscription_type_id format: {type_filter_id_str}")

        # 3. فلتر المصدر (source)
        if source_filter and source_filter.strip().lower() != "all":
            # إذا كان المصدر يمكن أن يكون فارغًا أو NULL في القاعدة وتريد فلترته
            if source_filter.strip().lower() == "none" or source_filter.strip().lower() == "null":
                where_clauses.append(f"(s.source IS NULL OR s.source = '')")
            else:
                where_clauses.append(f"s.source ILIKE ${len(where_params) + 1}")
                where_params.append(source_filter)  # يمكنك إضافة % إذا أردت بحث جزئي: f"%{source_filter}%"

        # 4. فلاتر تاريخ الانتهاء (expiry_date)
        if start_date_filter and start_date_filter.strip():
            where_clauses.append(f"s.expiry_date >= ${len(where_params) + 1}::DATE")
            where_params.append(start_date_filter)

        if end_date_filter and end_date_filter.strip():
            where_clauses.append(
                f"s.expiry_date < (${len(where_params) + 1}::DATE + INTERVAL '1 day')")  # غير شامل لليوم التالي
            where_params.append(end_date_filter)

        # 5. فلتر البحث (Search term)
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = []
            # يجب أن تبدأ معاملات البحث من حيث انتهت where_params
            current_param_idx = len(where_params)

            # البحث في telegram_id كـ TEXT
            search_conditions.append(f"s.telegram_id::TEXT ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)  # أضف المعامل إلى where_params
            current_param_idx += 1

            # البحث في full_name
            search_conditions.append(f"u.full_name ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            current_param_idx += 1

            # البحث في username
            search_conditions.append(f"u.username ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            # لا حاجة لزيادة current_param_idx هنا لأنه آخر معامل بحث

            where_clauses.append(f"({' OR '.join(search_conditions)})")

        # --- بناء جملة WHERE النهائية ---
        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0]  #

        # --- استعلام البيانات الرئيسي ---
        query_final_params = list(where_params)  # انسخ where_params لإنشاء query_final_params
        query = f"""
            {base_query_select}
            WHERE {where_sql}
            {order_by_clause}
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.extend([page_size, offset])

        # --- استعلام العد ---
        # count_params يجب أن تكون مطابقة لـ where_params المستخدمة في جملة WHERE للاستعلام الرئيسي
        count_query = f"""
            {count_base_query_select}
            WHERE {where_sql}
        """

        # --- إحصائية لعدد الاشتراكات النشطة (المفلترة بنفس الفلاتر الرئيسية، ما عدا فلتر الحالة is_active نفسه) ---
        # نبني where_clauses و where_params جديدة لهذا الإحصاء
        stat_where_clauses = []
        stat_where_params = []
        param_counter_for_stat = 1

        # أضف جميع الفلاتر ما عدا فلتر الحالة s.is_active
        if type_filter_id_str and type_filter_id_str.strip().lower() != "all":
            try:
                stat_where_clauses.append(f"s.subscription_type_id = ${param_counter_for_stat}")
                stat_where_params.append(int(type_filter_id_str))
                param_counter_for_stat += 1
            except ValueError:
                pass

        if source_filter and source_filter.strip().lower() != "all":
            if source_filter.strip().lower() == "none" or source_filter.strip().lower() == "null":
                stat_where_clauses.append(f"(s.source IS NULL OR s.source = '')")
            else:
                stat_where_clauses.append(f"s.source ILIKE ${param_counter_for_stat}")
                stat_where_params.append(source_filter)
                param_counter_for_stat += 1

        if start_date_filter and start_date_filter.strip():
            stat_where_clauses.append(f"s.expiry_date >= ${param_counter_for_stat}::DATE")
            stat_where_params.append(start_date_filter)
            param_counter_for_stat += 1

        if end_date_filter and end_date_filter.strip():
            stat_where_clauses.append(f"s.expiry_date < (${param_counter_for_stat}::DATE + INTERVAL '1 day')")
            stat_where_params.append(end_date_filter)
            param_counter_for_stat += 1

        if search_term:
            search_pattern_stat = f"%{search_term}%"  # استخدام نفس النمط
            stat_search_conditions = []

            stat_search_conditions.append(f"s.telegram_id::TEXT ILIKE ${param_counter_for_stat}")
            stat_where_params.append(search_pattern_stat)
            param_counter_for_stat += 1

            stat_search_conditions.append(f"u.full_name ILIKE ${param_counter_for_stat}")
            stat_where_params.append(search_pattern_stat)
            param_counter_for_stat += 1

            stat_search_conditions.append(f"u.username ILIKE ${param_counter_for_stat}")
            stat_where_params.append(search_pattern_stat)
            param_counter_for_stat += 1
            stat_where_clauses.append(f"({' OR '.join(stat_search_conditions)})")

        # أضف شرط أن الاشتراك نشط دائمًا لهذا الإحصاء
        stat_where_clauses.append(f"s.is_active = ${param_counter_for_stat}")
        stat_where_params.append(True)
        # param_counter_for_stat += 1 # لا داعي للزيادة هنا

        active_subscriptions_stat_where_sql = " AND ".join(stat_where_clauses) if stat_where_clauses else "1=1"
        # إذا لم يكن هناك أي فلاتر أخرى، فقط is_active = true
        if not stat_where_clauses:  # يعني فقط s.is_active = $1
            active_subscriptions_stat_where_sql = f"s.is_active = $1"  # المعامل هو True

        # تأكد من أن active_subscriptions_stat_where_sql ليست فارغة إذا لم تكن هناك أي شروط أخرى غير is_active
        if not active_subscriptions_stat_where_sql.strip() and True in stat_where_params:  # فقط is_active = true
            active_subscriptions_stat_where_sql = f"s.is_active = $1"

        active_subscriptions_stat_query = f"""
            SELECT COUNT(s.id) as active_total
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
            WHERE {active_subscriptions_stat_where_sql if active_subscriptions_stat_where_sql.strip() else 's.is_active = TRUE'} 
        """  # استخدام TRUE إذا كان where_sql فارغًا لضمان جلب جميع النشطين
        # إذا كان stat_where_params فارغًا بسبب عدم وجود فلاتر، فإن active_subscriptions_stat_where_sql سيكون فارغًا أيضًا.
        # في هذه الحالة، نريد عد جميع الاشتراكات النشطة.
        # إذا كان active_subscriptions_stat_where_sql فارغًا، و stat_where_params فارغًا أيضًا، هذا يعني لا فلاتر أخرى، فقط is_active=true
        if not active_subscriptions_stat_where_sql.strip() and not stat_where_params:
            active_subscriptions_stat_query = active_subscriptions_stat_query.replace("WHERE  AND",
                                                                                      "WHERE")  # تنظيف بسيط
            # لا يمكن أن يكون active_subscriptions_stat_where_sql فارغًا إذا كان stat_where_params يحتوي على True
            # الحالة الوحيدة التي يكون فيها فارغًا هي عدم وجود أي فلاتر أخرى + لم يتم إضافة is_active = true بعد
            # لكن الكود أعلاه يضيف is_active = true دائمًا.
            # الأمان:
            if not stat_where_params and "WHERE " == active_subscriptions_stat_query.strip()[
                                                     -6:]:  # إذا انتهى بـ WHERE وفراغ
                active_subscriptions_stat_query = active_subscriptions_stat_query.replace("WHERE ",
                                                                                          "WHERE s.is_active = TRUE ")

        items_data = []
        total_records = 0
        active_subscriptions_total_stat = 0

        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(query, *query_final_params)
            items_data = [dict(row) for row in rows]

            count_row = await conn.fetchrow(count_query, *where_params)
            total_records = count_row['total'] if count_row and count_row['total'] is not None else 0

            active_stat_row = await conn.fetchrow(active_subscriptions_stat_query, *stat_where_params)
            active_subscriptions_total_stat = active_stat_row['active_total'] if active_stat_row and active_stat_row[
                'active_total'] is not None else 0

        return jsonify({
            "data": items_data,
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "active_subscriptions_count": active_subscriptions_total_stat
        })

    except ValueError as ve:
        logging.error(f"Value error in /subscriptions: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters", "details": str(ve)}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /subscriptions: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed", "details": str(pe)}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /subscriptions: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# واجهة API جديدة للحصول على قائمة مصادر الاشتراكات المتاحة
@admin_routes.route("/subscription_sources", methods=["GET"])
@permission_required("pending_subscriptions.read")
async def get_subscription_sources():
    try:
        query = """
            SELECT DISTINCT source
            FROM subscriptions
            WHERE source IS NOT NULL
            ORDER BY source
        """

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query)

        sources = [row['source'] for row in rows if row['source']]
        return jsonify(sources)

    except Exception as e:
        logging.error("Error fetching subscription sources: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# واجهة API جديدة للحصول على pending_subscriptions
@admin_routes.route("/pending_subscriptions/stats", methods=["GET"])
@permission_required("pending_subscriptions.read")
async def get_pending_subscriptions_stats():
    try:
        async with current_app.db_pool.acquire() as connection:
            # جلب عدد الاشتراكات لكل حالة ذات أهمية
            query = """
                SELECT
                    status,
                    COUNT(*) AS count
                FROM pending_subscriptions
                GROUP BY status;
            """
            rows = await connection.fetch(query)

            stats = {row['status']: row['count'] for row in rows}
            # تأكد من القيم الافتراضية للحالات التي نهتم بها
            stats.setdefault('pending', 0)
            stats.setdefault('complete', 0)
            # يمكنك إضافة 'rejected' إذا كنت لا تزال تستخدمها
            # stats.setdefault('rejected', 0)

            # العدد الإجمالي للاشتراكات المعلقة (بجميع حالاتها)
            total_all_query = "SELECT COUNT(*) FROM pending_subscriptions;"
            total_all_count = await connection.fetchval(total_all_query)
            stats['total_all'] = total_all_count or 0

        return jsonify(stats)

    except Exception as e:
        logging.error("Error fetching pending subscriptions stats: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/pending_subscriptions", methods=["GET"])
@permission_required("pending_subscriptions.read")
async def get_pending_subscriptions_endpoint():  # تم تغيير الاسم ليناسب endpoint
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        # الفلاتر الجديدة
        status_filter = request.args.get("status", "all").lower()  # "pending", "complete", "all"
        # يمكنك إضافة فلاتر تاريخ لـ found_at إذا لزم الأمر
        # start_date_filter = request.args.get("start_date")
        # end_date_filter = request.args.get("end_date")

        order_by_clause = "ORDER BY ps.found_at DESC"

        base_query_select = """
            SELECT
                ps.id, ps.user_db_id, ps.telegram_id, ps.channel_id, 
                ps.subscription_type_id, ps.found_at, ps.status,
                ps.admin_reviewed_at,
                u.full_name, u.username,
                st.name AS subscription_type_name
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.user_db_id = u.id
            LEFT JOIN subscription_types st ON ps.subscription_type_id = st.id
        """
        count_base_query_select = """
            SELECT COUNT(ps.id) as total
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.user_db_id = u.id
            LEFT JOIN subscription_types st ON ps.subscription_type_id = st.id
        """

        where_clauses = ["1=1"]
        where_params = []

        # 1. فلتر الحالة (status)
        if status_filter != "all":
            where_clauses.append(f"ps.status = ${len(where_params) + 1}")
            where_params.append(status_filter)

        # 2. فلتر البحث
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = []
            current_param_idx = len(where_params)

            search_conditions.append(f"u.full_name ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            current_param_idx += 1

            search_conditions.append(f"u.username ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)
            current_param_idx += 1

            search_conditions.append(f"ps.telegram_id::TEXT ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern)

            where_clauses.append(f"({' OR '.join(search_conditions)})")

        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0]

        query_final_params = list(where_params)
        query = f"""
            {base_query_select}
            WHERE {where_sql}
            {order_by_clause}
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.extend([page_size, offset])

        count_query = f"""
            {count_base_query_select}
            WHERE {where_sql}
        """

        # إحصائية لعدد الاشتراكات المعلقة التي تطابق الفلتر الحالي (status, search)
        # هذا هو نفس استعلام العد الرئيسي، لذا يمكن استخدام نتيجته مباشرة كإحصائية
        # إذا أردت إحصائية مختلفة (مثلاً، إجمالي المعلقين بغض النظر عن فلتر البحث)، ستحتاج استعلامًا آخر.
        # حاليًا، `total_records` سيكون هو نفسه `pending_count_for_current_filter`.

        items_data = []
        total_records = 0

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *query_final_params)
            items_data = [dict(row) for row in rows]

            count_row = await connection.fetchrow(count_query, *where_params)
            total_records = count_row['total'] if count_row and count_row['total'] is not None else 0

        # الإحصائية هنا هي نفسها `total_records` لأنها تعكس العدد المطابق للفلاتر الحالية
        pending_count_stat = total_records

        return jsonify({
            "data": items_data,
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "pending_count_for_filter": pending_count_stat
        })

    except ValueError as ve:
        logging.error(f"Value error in /pending_subscriptions: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters", "details": str(ve)}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /pending_subscriptions: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed", "details": str(pe)}), 500
    except Exception as e:
        logging.error("Error fetching pending subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# واجهة API إضافية للتعامل مع إجراءات pending_subscriptions
@admin_routes.route("/pending_subscriptions/<int:record_id>/action", methods=["POST"])  # تغيير 'id' إلى 'record_id'
@permission_required("pending_subscriptions.remove_single")
async def handle_single_pending_subscription(record_id: int):  # تغيير 'id' إلى 'record_id' وإضافة type hint
    try:
        if not request.is_json:
            return jsonify({"error": "Request body must be JSON"}), 415

        data = await request.get_json()
        if data is None:
            return jsonify({"error": "Invalid JSON payload"}), 400

        action = data.get('action')
        if action != 'mark_as_complete':
            return jsonify({"error": f"Invalid action: '{action}'. Expected 'mark_as_complete'."}), 400

        async with current_app.db_pool.acquire() as connection:
            # جلب ps.telegram_id و ps.channel_id مباشرة من pending_subscriptions
            pending_sub = await connection.fetchrow(
                """
                SELECT ps.id, ps.telegram_id, ps.channel_id, ps.status
                FROM pending_subscriptions ps
                WHERE ps.id = $1
                """, record_id  # استخدام الاسم الجديد
            )

            if not pending_sub:
                return jsonify({"error": "Pending subscription not found"}), 404

            if pending_sub['status'] == 'complete':
                return jsonify({"success": True, "message": "Subscription already marked as complete."}), 200

            if pending_sub['status'] != 'pending':
                return jsonify({
                    "error": f"Subscription is not in 'pending' state (current: {pending_sub['status']}). Cannot mark as complete."}), 400

            telegram_id_to_remove = pending_sub['telegram_id']
            channel_id_to_remove_from = pending_sub['channel_id']

            # استخدام الهيكل المبسّط الذي اقترحته سابقًا
            try:
                bot_removed_user = await remove_users_from_channel(
                    telegram_id=telegram_id_to_remove,
                    channel_id=channel_id_to_remove_from
                )

                if not bot_removed_user:
                    specific_error_msg = f"Bot failed to remove user {telegram_id_to_remove} from channel {channel_id_to_remove_from} or send notification."
                    logging.warning(specific_error_msg + " Status will not be updated to complete.")
                    return jsonify({"error": specific_error_msg, "bot_action_failed": True}), 500  # أو 422

                await connection.execute("""
                    UPDATE pending_subscriptions
                    SET status = 'complete', admin_reviewed_at = NOW()
                    WHERE id = $1
                """, record_id)  # استخدام الاسم الجديد

                return jsonify({"success": True,
                                "message": f"Pending subscription for user {telegram_id_to_remove} marked as complete. User removed and notified."})

            except Exception as e:
                exception_error_msg = f"Exception during bot action or DB update for sub {record_id} (User {telegram_id_to_remove}): {e}"
                logging.error(exception_error_msg, exc_info=True)
                return jsonify({"error": exception_error_msg, "bot_action_failed": True}), 500

    except Exception as e:  # هذا يلتقط الأخطاء قبل الوصول إلى منطق المعالجة الرئيسي
        logging.error(f"Error in handle_single_pending_subscription for ID {record_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/pending_subscriptions/bulk_action", methods=["POST"])
@permission_required("pending_subscriptions.remove_bulk")
async def handle_bulk_pending_subscriptions_action():  # تم تغيير الاسم قليلاً
    try:
        if not request.is_json:
            return jsonify({"error": "Request body must be JSON"}), 415

        data = await request.get_json()
        if data is None:
            return jsonify({"error": "Invalid JSON payload"}), 400

        action = data.get('action')
        filter_criteria = data.get('filter', {})

        if action != 'mark_as_complete':
            return jsonify({"error": f"Invalid action: '{action}'. Expected 'mark_as_complete'."}), 400

        async with current_app.db_pool.acquire() as connection:
            query_conditions = ["ps.status = 'pending'"]
            query_params = []

            if 'channel_id' in filter_criteria:
                query_conditions.append(f"ps.channel_id = ${len(query_params) + 1}")
                query_params.append(filter_criteria['channel_id'])

            if 'subscription_type_id' in filter_criteria:
                query_conditions.append(f"ps.subscription_type_id = ${len(query_params) + 1}")
                query_params.append(filter_criteria['subscription_type_id'])

            where_clause = " AND ".join(query_conditions)

            # جلب ps.telegram_id و ps.channel_id مباشرة
            query = f"""
                SELECT ps.id, ps.telegram_id, ps.channel_id
                FROM pending_subscriptions ps 
                WHERE {where_clause}
            """
            pending_subs_to_process = await connection.fetch(query, *query_params)

            if not pending_subs_to_process:
                return jsonify({"success": True, "message": "No pending subscriptions match the criteria."}), 200

            succeeded_updates = 0
            failed_bot_actions = 0
            processed_candidates = len(pending_subs_to_process)
            failures_details = []

            for sub_data in pending_subs_to_process:
                sub_id = sub_data['id']
                telegram_id_to_remove = sub_data['telegram_id']
                channel_id_to_remove_from = sub_data['channel_id']

                try:
                    bot_removed_user = await remove_users_from_channel(

                        telegram_id=telegram_id_to_remove,
                        channel_id=channel_id_to_remove_from
                    )
                    if bot_removed_user:
                        await connection.execute("""
                            UPDATE pending_subscriptions
                            SET status = 'complete', admin_reviewed_at = NOW()
                            WHERE id = $1
                        """, sub_id)
                        succeeded_updates += 1
                        logging.info(f"Bulk: Successfully processed sub_id {sub_id} (User {telegram_id_to_remove})")
                    else:
                        failed_bot_actions += 1
                        bot_error_info = f"Bot action failed for user {telegram_id_to_remove} in channel {channel_id_to_remove_from}."
                        logging.warning(f"Bulk: {bot_error_info} Sub_id {sub_id} not marked complete.")
                        failures_details.append(
                            {"sub_id": sub_id, "telegram_id": telegram_id_to_remove, "error": bot_error_info})

                except Exception as e:
                    failed_bot_actions += 1
                    bot_error_info = f"Exception during bot action or DB update for sub {sub_id} (User {telegram_id_to_remove}): {str(e)}"
                    logging.error(f"Bulk: {bot_error_info}", exc_info=True)
                    failures_details.append(
                        {"sub_id": sub_id, "telegram_id": telegram_id_to_remove, "error": bot_error_info})

            result_message = (
                f"Bulk processing: Candidates: {processed_candidates}. "
                f"Succeeded: {succeeded_updates}. Failures: {failed_bot_actions}."
            )
            return jsonify({
                "success": True,
                "message": result_message,
                "details": {
                    "total_candidates": processed_candidates,
                    "successful_updates": succeeded_updates,
                    "failed_bot_or_db_updates": failed_bot_actions,
                    "failures_log": failures_details
                }
            })
    except Exception as e:
        logging.error("Error in bulk_action: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error during bulk processing"}), 500


@admin_routes.route("/legacy_subscriptions", methods=["GET"])
@permission_required("legacy_subscriptions.read")
async def get_legacy_subscriptions_endpoint():  # تم تغيير الاسم
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        # الفلاتر الجديدة
        processed_filter_str = request.args.get("processed")  # "true", "false", "all"
        # يمكنك إضافة فلاتر تاريخ لـ expiry_date إذا لزم الأمر
        # start_date_filter = request.args.get("start_date")
        # end_date_filter = request.args.get("end_date")

        # الفرز: للتوحيد، نستخدم فرزًا افتراضيًا.
        order_by_clause = "ORDER BY ls.expiry_date DESC"  # أو ls.id DESC

        base_query_select = """
    SELECT
        ls.id,
        ls.username,
        ls.subscription_type_id,
        ls.start_date,
        ls.expiry_date,
        ls.processed,
        st.name AS subscription_type_name
    FROM legacy_subscriptions ls
    LEFT JOIN subscription_types st ON ls.subscription_type_id = st.id
"""
        count_base_query_select = """
            SELECT COUNT(ls.id) as total
            FROM legacy_subscriptions ls
            LEFT JOIN subscription_types st ON ls.subscription_type_id = st.id
        """

        where_clauses = ["1=1"]
        where_params = []

        # 1. فلتر المعالجة (processed)
        if processed_filter_str and processed_filter_str.lower() != "all":
            processed_bool = processed_filter_str.lower() == "true"
            where_clauses.append(f"ls.processed = ${len(where_params) + 1}")
            where_params.append(processed_bool)

        # 2. فلتر البحث (search_term على username)
        if search_term:
            search_pattern = f"%{search_term}%"
            # البحث فقط في username لـ legacy_subscriptions كما كان في الكود الأصلي
            # إذا أردت البحث في حقول أخرى، أضفها هنا
            where_clauses.append(f"ls.username ILIKE ${len(where_params) + 1}")
            where_params.append(search_pattern)

        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0]

        query_final_params = list(where_params)
        query = f"""
            {base_query_select}
            WHERE {where_sql}
            {order_by_clause}
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.extend([page_size, offset])

        count_query = f"""
            {count_base_query_select}
            WHERE {where_sql}
        """

        # إحصائية لعدد الاشتراكات القديمة المعالجة (المفلترة بنفس فلتر البحث، ولكن دائمًا processed=true)
        stat_where_clauses_processed = []
        stat_where_params_processed = []
        param_counter_stat_proc = 1

        if search_term:
            stat_where_clauses_processed.append(f"ls.username ILIKE ${param_counter_stat_proc}")
            stat_where_params_processed.append(f"%{search_term}%")
            param_counter_stat_proc += 1

        stat_where_clauses_processed.append(f"ls.processed = ${param_counter_stat_proc}")
        stat_where_params_processed.append(True)

        processed_stat_where_sql = " AND ".join(stat_where_clauses_processed) if stat_where_clauses_processed else "1=1"
        # إذا لم يكن هناك بحث، فقط processed = true
        if not search_term:
            processed_stat_where_sql = "ls.processed = $1"

        processed_legacy_stat_query = f"""
            SELECT COUNT(ls.id) as processed_total
            FROM legacy_subscriptions ls
            WHERE {processed_stat_where_sql}
        """

        items_data = []
        total_records = 0
        processed_legacy_total_stat = 0

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *query_final_params)
            items_data = [dict(row) for row in rows]

            count_row = await connection.fetchrow(count_query, *where_params)
            total_records = count_row['total'] if count_row and count_row['total'] is not None else 0

            processed_stat_row = await connection.fetchrow(processed_legacy_stat_query, *stat_where_params_processed)
            processed_legacy_total_stat = processed_stat_row['processed_total'] if processed_stat_row and \
                                                                                   processed_stat_row[
                                                                                       'processed_total'] is not None else 0

        return jsonify({
            "data": items_data,
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "processed_legacy_count": processed_legacy_total_stat
        })

    except ValueError as ve:
        logging.error(f"Value error in /legacy_subscriptions: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters", "details": str(ve)}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /legacy_subscriptions: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed", "details": str(pe)}), 500
    except Exception as e:
        logging.error("Error fetching legacy subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# واجهة API للحصول على إحصائيات legacy_subscriptions (تبقى كما هي إذا كنت تستدعيها بشكل منفصل)
# ولكن الكود الجديد في الواجهة الأمامية يجلب الإحصائيات من خلال getLegacySubscriptions نفسه
# لذا قد لا تكون هذه الـ endpoint ضرورية إذا اعتمدت على الواجهة الأمامية الجديدة
@admin_routes.route("/legacy_subscriptions/stats", methods=["GET"])
@permission_required("legacy_subscriptions.read")
async def get_legacy_subscription_stats():
    try:
        # يمكنك إضافة فلاتر هنا إذا كنت تريد إحصائيات مفلترة
        processed_filter = request.args.get("processed")  # "true", "false", or None

        query_base = "FROM legacy_subscriptions"
        query_conditions = ""
        params = []

        if processed_filter is not None:
            query_conditions += " WHERE processed = $1"
            params.append(processed_filter.lower() == "true")

        # استعلام واحد للإحصائيات
        query = f"""
            SELECT 
                COUNT(*) FILTER (WHERE processed = true {'AND processed = $1' if processed_filter == 'true' else ''} {'AND processed = $1' if processed_filter == 'false' else ''} ) AS processed_true,
                COUNT(*) FILTER (WHERE processed = false {'AND processed = $1' if processed_filter == 'true' else ''} {'AND processed = $1' if processed_filter == 'false' else ''} ) AS processed_false,
                COUNT(*) AS total
            {query_base} {query_conditions}
        """
        # تبسيط منطق الإحصائيات إذا كانت الـ endpoint مخصصة فقط للإحصائيات الإجمالية أو المفلترة

        # إذا كنت تريد فقط الإحصائيات الإجمالية (غير مفلترة):
        if processed_filter is None:
            query = """
                SELECT 
                    COUNT(*) FILTER (WHERE processed = true) AS processed_true,
                    COUNT(*) FILTER (WHERE processed = false) AS processed_false,
                    COUNT(*) AS total
                FROM legacy_subscriptions
            """
            params = []  # لا معلمات للاستعلام الإجمالي
        elif processed_filter == "true":
            query = "SELECT COUNT(*) AS count FROM legacy_subscriptions WHERE processed = true"
        elif processed_filter == "false":
            query = "SELECT COUNT(*) AS count FROM legacy_subscriptions WHERE processed = false"

        async with current_app.db_pool.acquire() as connection:
            # هذا الجزء يحتاج إلى تعديل بناءً على ما إذا كنت تريد إحصائيات مفلترة أم لا من هذه الـ endpoint
            # الكود الأمامي الجديد يستدعي /legacy_subscriptions مع params للحصول على الأعداد من هناك
            # لذا، هذه الـ endpoint قد تكون للإحصائيات العامة فقط

            # للحصول على إحصائيات عامة (كما كان في الأصل):
            overall_stats_query = """
                SELECT 
                    COUNT(*) FILTER (WHERE processed = true) AS processed_true,
                    COUNT(*) FILTER (WHERE processed = false) AS processed_false,
                    COUNT(*) AS total
                FROM legacy_subscriptions
            """
            stats = await connection.fetchrow(overall_stats_query)

        return jsonify({
            "true": stats["processed_true"] if stats else 0,  # مطابقة للمفتاح في React
            "false": stats["processed_false"] if stats else 0,  # مطابقة للمفتاح في React
            "total": stats["total"] if stats else 0
        })

    except Exception as e:
        logging.error("Error fetching legacy subscription stats: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# =====================================
# 2. API لجلب بيانات الدفعات مع دعم الفلاتر والتجزئة والتقارير المالية
# =====================================

@admin_routes.route("/payments", methods=["GET"])
@permission_required("payments.read_all")
async def get_payments():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size

        # استخراج الفلاتر من الطلب
        status_filter = request.args.get("status")
        user_id_filter_str = request.args.get("user_id")
        start_date_filter = request.args.get("start_date")
        end_date_filter = request.args.get("end_date")
        search_term = request.args.get("search", "")

        base_query_select = """
            SELECT 
                p.id, p.user_id, p.subscription_plan_id, p.amount, p.created_at, 
                p.status, p.tx_hash, p.telegram_id, p.username, p.full_name,
                p.user_wallet_address, p.payment_method, p.payment_token,
                p.currency, p.amount_received, p.error_message, p.expires_at,
                p.processed_at,
                sp.name AS plan_name,
                st.name AS subscription_type_name
            FROM payments p
            LEFT JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
            LEFT JOIN subscription_types st ON sp.subscription_type_id = st.id
        """

        count_base_query_select = """
            SELECT COUNT(p.id) as total
            FROM payments p
            LEFT JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
            LEFT JOIN subscription_types st ON sp.subscription_type_id = st.id
        """

        where_clauses = ["1=1"]
        # هذه هي المعاملات التي ستُستخدم في جملة WHERE فقط
        # لن تحتوي على معاملات LIMIT أو OFFSET
        where_params = []

        # 1. فلتر الحالة (Status)
        if status_filter and status_filter.lower() != "all":
            where_clauses.append(f"p.status = ${len(where_params) + 1}")
            where_params.append(status_filter)
        else:
            # إذا لم يتم تحديد فلتر حالة (أو كان 'all')
            default_statuses = ["'completed'", "'failed'"]
            # إذا كان هناك بحث، قم بتضمين 'pending' في نطاق البحث الافتراضي للحالة
            if search_term:
                default_statuses.append("'pending'")
            where_clauses.append(f"p.status IN ({', '.join(default_statuses)})")
            # لا توجد معاملات تُضاف هنا لـ where_params لأن الحالات مدمجة مباشرة في الاستعلام

        # 2. فلتر معرّف المستخدم (User ID)
        if user_id_filter_str and user_id_filter_str.strip():
            try:
                user_id_val = int(user_id_filter_str)
                where_clauses.append(f"p.user_id = ${len(where_params) + 1}")
                where_params.append(user_id_val)
            except ValueError:
                logging.warning(f"Invalid user_id format received: {user_id_filter_str}")
                # يمكنك اختيار إرجاع خطأ 400 أو تجاهل الفلتر
                # return jsonify({"error": "Invalid user_id format"}), 400

        # 3. فلاتر التاريخ (Date filters)
        if start_date_filter and start_date_filter.strip():
            where_clauses.append(f"p.created_at >= ${len(where_params) + 1}::TIMESTAMP")
            where_params.append(start_date_filter)

        if end_date_filter and end_date_filter.strip():
            where_clauses.append(f"p.created_at <= ${len(where_params) + 1}::TIMESTAMP")
            where_params.append(end_date_filter)

        # 4. فلتر البحث (Search term)
        # يجب أن يكون هذا بعد إضافة المعاملات الأخرى المحتملة إلى where_params
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = []
            # المعاملات التي ستُضاف هنا هي فقط لمعايير البحث

            # يجب أن يبدأ ترقيم المعاملات من حيث انتهى where_params
            current_param_index = len(where_params)

            search_conditions.append(f"p.tx_hash ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)  # أضف المعامل إلى where_params
            current_param_index += 1

            search_conditions.append(f"p.payment_token ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            current_param_index += 1

            search_conditions.append(f"p.username ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            current_param_index += 1

            # telegram_id هو BIGINT، لذا قم بتحويله إلى TEXT لـ ILIKE
            search_conditions.append(f"p.telegram_id::TEXT ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            # current_param_index += 1 # لا حاجة لزيادته هنا لأنه آخر معامل بحث

            where_clauses.append(f"({' OR '.join(search_conditions)})")

        # --- بناء جملة WHERE النهائية ---
        where_sql = " AND ".join(where_clauses)

        # --- استعلام البيانات الرئيسي ---
        # انسخ where_params لإنشاء query_final_params التي ستحتوي أيضًا على معاملات LIMIT و OFFSET
        query_final_params = list(where_params)

        query = f"""
            {base_query_select}
            WHERE {where_sql}
            ORDER BY p.created_at DESC 
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.append(page_size)
        query_final_params.append(offset)

        # --- استعلام العد ---
        # count_params يجب أن تكون مطابقة لـ where_params المستخدمة في جملة WHERE للاستعلام الرئيسي
        count_query = f"""
            {count_base_query_select}
            WHERE {where_sql}
        """

        rows = []
        total_records = 0
        completed_payments_count = 0

        async with current_app.db_pool.acquire() as connection:
            # تنفيذ استعلام البيانات
            rows_result = await connection.fetch(query, *query_final_params)
            rows = [dict(row) for row in rows_result]  # تحويل السجلات إلى قواميس هنا

            # تنفيذ استعلام العدد
            count_result_row = await connection.fetchrow(count_query, *where_params)  # استخدم where_params هنا
            if count_result_row and "total" in count_result_row:
                total_records = count_result_row["total"]

            # إحصائية عدد المدفوعات المكتملة (هذه عالمية وليست مفلترة حاليًا)
            # إذا أردت أن تكون مفلترة، ستحتاج لإضافة where_sql و where_params إليها أيضًا
            completed_count_query_sql = """
                SELECT COUNT(id) as completed_count
                FROM payments
                WHERE status = 'completed'
            """
            completed_count_result_row = await connection.fetchrow(completed_count_query_sql)
            if completed_count_result_row and "completed_count" in completed_count_result_row:
                completed_payments_count = completed_count_result_row["completed_count"]

        # التقارير المالية (مثال: إجمالي الإيرادات)
        total_revenue = 0
        if request.args.get("report") == "total_revenue":
            # هذا الاستعلام حاليًا يستخدم فلاتر محدودة خاصة به.
            # إذا أردت أن يتبع نفس الفلاتر الرئيسية، ستحتاج لتعديله ليشبه استعلام العد.
            rev_query_clauses = ["1=1"]
            rev_params = []
            if status_filter and status_filter.lower() != "all":  # استخدام status_filter من بداية الدالة
                rev_query_clauses.append(f"status = ${len(rev_params) + 1}")
                rev_params.append(status_filter)

            # يمكنك إضافة فلاتر user_id و dates هنا بنفس الطريقة إذا كانت مطلوبة للإيرادات
            if user_id_filter_str and user_id_filter_str.strip():
                try:
                    user_id_val_rev = int(user_id_filter_str)
                    rev_query_clauses.append(f"user_id = ${len(rev_params) + 1}")
                    rev_params.append(user_id_val_rev)
                except ValueError:
                    pass  # تجاهل إذا كان غير صالح لهذا الاستعلام المحدد

            if start_date_filter and start_date_filter.strip():
                rev_query_clauses.append(f"created_at >= ${len(rev_params) + 1}::TIMESTAMP")
                rev_params.append(start_date_filter)

            if end_date_filter and end_date_filter.strip():
                rev_query_clauses.append(f"created_at <= ${len(rev_params) + 1}::TIMESTAMP")
                rev_params.append(end_date_filter)

            rev_where_sql = " AND ".join(rev_query_clauses)
            rev_query = f"SELECT SUM(amount) as total FROM payments WHERE {rev_where_sql}"

            async with current_app.db_pool.acquire() as connection:
                rev_row = await connection.fetchrow(rev_query, *rev_params)
            if rev_row and rev_row["total"] is not None:
                total_revenue = rev_row["total"]
            else:
                total_revenue = 0

        response = {
            "data": rows,  # rows تم تحويلها إلى قواميس بالفعل
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "completed_count": completed_payments_count,
            "total_revenue": float(total_revenue) if total_revenue else 0.0  # تأكد من أن الإيرادات رقم عشري
        }
        return jsonify(response)

    except ValueError as ve:  # لالتقاط أخطاء تحويل page/page_size إلى int
        logging.error(f"Invalid parameter format: {ve}", exc_info=True)
        return jsonify({"error": "Invalid parameter format", "details": str(ve)}), 400
    except Exception as e:
        logging.error("Error fetching payments: %s", e, exc_info=True)
        # في الإنتاج، قد لا ترغب في إرسال تفاصيل الخطأ للعميل
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@admin_routes.route("/incoming-transactions", methods=["GET"])
@permission_required("payments.read_incoming_transactions")
async def get_incoming_transactions():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size

        processed_filter_str = request.args.get("processed")
        start_date_filter = request.args.get("start_date")
        end_date_filter = request.args.get("end_date")
        search_term = request.args.get("search", "").strip()

        base_query_select = """
            SELECT 
                it.txhash, it.sender_address, it.amount, it.payment_token, 
                it.processed, it.received_at, it.memo, it.txhash_base64
            FROM incoming_transactions it
        """
        count_base_query_select = """
            SELECT COUNT(it.txhash) as total
            FROM incoming_transactions it
        """

        # --- بناء جملة WHERE والمعاملات للاستعلام الرئيسي ---
        main_where_clauses = []
        main_where_params = []
        param_idx_main = 1  # لبدء ترقيم المعاملات من $1

        # 1. فلتر الحالة (Processed) للاستعلام الرئيسي
        if processed_filter_str and processed_filter_str.lower() != "all":
            try:
                processed_val = processed_filter_str.lower() == "true"
                main_where_clauses.append(f"it.processed = ${param_idx_main}")
                main_where_params.append(processed_val)
                param_idx_main += 1
            except ValueError:
                logging.warning(f"Invalid processed filter value: {processed_filter_str}")

        # 2. فلاتر التاريخ للاستعلام الرئيسي
        if start_date_filter and start_date_filter.strip():
            main_where_clauses.append(f"it.received_at >= ${param_idx_main}::TIMESTAMP")
            main_where_params.append(start_date_filter)
            param_idx_main += 1
        if end_date_filter and end_date_filter.strip():
            main_where_clauses.append(f"it.received_at <= ${param_idx_main}::TIMESTAMP")
            main_where_params.append(end_date_filter)
            param_idx_main += 1

        # 3. فلتر البحث للاستعلام الرئيسي
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions_main = []
            search_fields = ["it.txhash", "it.sender_address", "it.memo", "it.payment_token"]
            for field in search_fields:
                search_conditions_main.append(f"{field} ILIKE ${param_idx_main}")
                main_where_params.append(search_pattern)
                param_idx_main += 1
            if search_conditions_main:
                main_where_clauses.append(f"({' OR '.join(search_conditions_main)})")

        main_where_sql = " AND ".join(main_where_clauses) if main_where_clauses else "1=1"

        # --- استعلام البيانات الرئيسي ---
        query_final_params = list(main_where_params)  # نسخة من المعاملات
        query = f"""
            {base_query_select}
            WHERE {main_where_sql}
            ORDER BY it.received_at DESC 
            LIMIT ${param_idx_main} OFFSET ${param_idx_main + 1}
        """
        query_final_params.extend([page_size, offset])

        # --- استعلام العد الكلي (المفلتر) ---
        count_query = f"""
            {count_base_query_select}
            WHERE {main_where_sql}
        """

        # --- بناء جملة WHERE والمعاملات لعد المعاملات المعالجة (processed_count) ---
        # هذا العد يجب أن يطبق جميع الفلاتر الأخرى (التاريخ، البحث) ثم يضيف شرط processed = true

        processed_count_clauses = []
        processed_count_params = []
        param_idx_processed_count = 1

        # أ. فلاتر التاريخ لعد المعاملات المعالجة
        if start_date_filter and start_date_filter.strip():
            processed_count_clauses.append(f"it.received_at >= ${param_idx_processed_count}::TIMESTAMP")
            processed_count_params.append(start_date_filter)
            param_idx_processed_count += 1
        if end_date_filter and end_date_filter.strip():
            processed_count_clauses.append(f"it.received_at <= ${param_idx_processed_count}::TIMESTAMP")
            processed_count_params.append(end_date_filter)
            param_idx_processed_count += 1

        # ب. فلتر البحث لعد المعاملات المعالجة
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions_processed_count = []
            search_fields_for_processed_count = ["it.txhash", "it.sender_address", "it.memo", "it.payment_token"]
            for field in search_fields_for_processed_count:
                search_conditions_processed_count.append(f"{field} ILIKE ${param_idx_processed_count}")
                processed_count_params.append(search_pattern)
                param_idx_processed_count += 1
            if search_conditions_processed_count:
                processed_count_clauses.append(f"({' OR '.join(search_conditions_processed_count)})")

        # ج. إضافة شرط أن المعاملة معالجة (processed = True) بشكل إلزامي لهذا العد
        processed_count_clauses.append(f"it.processed = ${param_idx_processed_count}")
        processed_count_params.append(True)
        # param_idx_processed_count += 1 # ليس ضروريًا بعد الآن

        processed_count_where_sql = " AND ".join(processed_count_clauses) if processed_count_clauses else "1=1"
        # بما أننا نضيف دائماً it.processed = $N، فلن تكون القائمة فارغة، لذا يمكننا إزالة "else '1=1'" إذا أردنا
        # ولكن للسلامة، يمكن تركها. الأصح هو `WHERE {processed_count_where_sql}` فقط لأنها لن تكون فارغة.

        processed_count_query_sql = f"""
            SELECT COUNT(it.txhash) as processed_count
            FROM incoming_transactions it
            WHERE {processed_count_where_sql}
        """

        rows_data = []
        total_records = 0
        processed_transactions_count = 0

        async with current_app.db_pool.acquire() as connection:
            rows_result = await connection.fetch(query, *query_final_params)
            rows_data = [dict(row) for row in rows_result]

            count_result_row = await connection.fetchrow(count_query, *main_where_params)
            if count_result_row and "total" in count_result_row:
                total_records = count_result_row["total"]

            processed_count_result_row = await connection.fetchrow(processed_count_query_sql, *processed_count_params)
            if processed_count_result_row and "processed_count" in processed_count_result_row:
                processed_transactions_count = processed_count_result_row["processed_count"]

        response = {
            "data": rows_data,
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "processed_count": processed_transactions_count,
        }
        return jsonify(response)

    except ValueError as ve:
        logging.error(f"Invalid parameter format: {ve}", exc_info=True)
        return jsonify({"error": "Invalid parameter format", "details": str(ve)}), 400
    except Exception as e:
        logging.error("Error fetching incoming transactions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# =====================================
# 3. API لتعديل اشتراك مستخدم
# =====================================
@admin_routes.route("/subscriptions/<int:subscription_id>", methods=["PUT"])
@permission_required("user_subscriptions.update")
async def update_subscription(subscription_id):
    try:
        data = await request.get_json()
        expiry_date = data.get("expiry_date")
        subscription_plan_id = data.get("subscription_plan_id")
        source = data.get("source")  # مثال: "manual" أو "auto"

        if expiry_date is None and subscription_plan_id is None and source is None:
            return jsonify({"error": "No fields provided for update"}), 400

        update_fields = []
        params = []
        idx = 1
        local_tz = pytz.timezone("Asia/Riyadh")

        if expiry_date:
            # تحويل expiry_date إلى datetime timezone-aware باستخدام local_tz
            dt_expiry = datetime.fromisoformat(expiry_date.replace("Z", "")).replace(tzinfo=pytz.UTC).astimezone(
                local_tz)
            update_fields.append(f"expiry_date = ${idx}")
            params.append(dt_expiry)
            idx += 1

            # إعادة حساب is_active بناءً على expiry_date
            is_active = dt_expiry > datetime.now(local_tz)
            update_fields.append(f"is_active = ${idx}")
            params.append(is_active)
            idx += 1

        if subscription_plan_id:
            update_fields.append(f"subscription_plan_id = ${idx}")
            params.append(subscription_plan_id)
            idx += 1

        if source:
            update_fields.append(f"source = ${idx}")
            params.append(source)
            idx += 1

        update_fields.append("updated_at = now()")
        query = f"UPDATE subscriptions SET {', '.join(update_fields)} WHERE id = ${idx} RETURNING *;"
        params.append(subscription_id)

        async with current_app.db_pool.acquire() as connection:
            row = await connection.fetchrow(query, *params)
        if not row:
            return jsonify({"error": "Subscription not found"}), 404
        return jsonify(dict(row))
    except Exception as e:
        logging.error("Error updating subscription: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# =====================================
# 4. API لإضافة اشتراك جديد
# =====================================
@admin_routes.route("/subscriptions", methods=["POST"])  # استخدام المسار الأصلي الذي لديك
@permission_required("user_subscriptions.create_manual")
async def add_subscription_admin():  # تم تغيير اسم الدالة لتمييزها
    try:
        data = await request.get_json()
        telegram_id_str = data.get("telegram_id")
        days_to_add_str = data.get("days_to_add")
        subscription_type_id_str = data.get("subscription_type_id")

        full_name = data.get("full_name")  # اسم المستخدم (اختياري، للتسجيل الأولي)
        username = data.get("username")  # اسم مستخدم تيليجرام (اختياري، للتسجيل الأولي)

        if not all([telegram_id_str, subscription_type_id_str, days_to_add_str]):
            return jsonify({"error": "Missing required fields: telegram_id, subscription_type_id, days_to_add"}), 400

        try:
            telegram_id = int(telegram_id_str)
            days_to_add = int(days_to_add_str)
            subscription_type_id = int(subscription_type_id_str)
            if days_to_add <= 0:
                return jsonify({"error": "days_to_add must be a positive integer"}), 400
        except ValueError:
            return jsonify({"error": "Invalid data type for telegram_id, subscription_type_id, or days_to_add"}), 400

        db_pool = getattr(current_app, "db_pool", None)
        if not db_pool:
            logging.critical("❌ Database connection is missing!")
            return jsonify({"error": "Internal Server Error"}), 500

        async with db_pool.acquire() as connection:
            # 1. تأكد من وجود المستخدم أو قم بإنشائه/تحديثه
            await add_user(connection, telegram_id, username=username, full_name=full_name)

            user_info_for_greeting = await connection.fetchrow(
                "SELECT username, full_name FROM users WHERE telegram_id = $1", telegram_id)
            actual_full_name = full_name or (
                user_info_for_greeting.get('full_name') if user_info_for_greeting else None)
            actual_username = username or (user_info_for_greeting.get('username') if user_info_for_greeting else None)
            greeting_name = actual_full_name or actual_username or str(telegram_id)

            # 2. احصل على معلومات نوع الاشتراك (اسم النوع، القناة الرئيسية)
            subscription_type_info = await connection.fetchrow(
                "SELECT name, channel_id AS main_channel_id FROM subscription_types WHERE id = $1",
                subscription_type_id
            )
            if not subscription_type_info:
                return jsonify({"error": "Invalid subscription_type_id"}), 400

            main_channel_id = int(subscription_type_info["main_channel_id"])
            subscription_type_name = subscription_type_info["name"]

            if not main_channel_id:
                logging.error(f"ADMIN: No main_channel_id for subscription_type_id: {subscription_type_id}")
                return jsonify({"error": "Subscription type is not configured with a main channel."}), 400

            # 3. حساب تواريخ الاشتراك
            current_time_utc = datetime.now(timezone.utc)
            calculated_start_date, calculated_new_expiry_date = await _calculate_admin_subscription_dates(
                connection, telegram_id, main_channel_id, days_to_add, current_time_utc
            )

            # 4. إنشاء رابط دعوة للقناة الرئيسية
            main_invite_result = await generate_channel_invite_link(telegram_id, main_channel_id,
                                                                    subscription_type_name)
            if not main_invite_result["success"]:
                logging.error(
                    f"ADMIN: Failed to generate invite link for main channel {main_channel_id}: {main_invite_result.get('error')}")
                return jsonify(
                    {"error": f"Failed to generate main invite link: {main_invite_result.get('error')}"}), 500
            main_invite_link = main_invite_result["invite_link"]

            # 5. تحديد مصدر العملية
            admin_action_source = "admin_manual"

            # 6. إضافة أو تحديث الاشتراك في جدول 'subscriptions' للقناة الرئيسية
            # `subscription_plan_id` و `payment_id` سيتم تمريرهما كـ None
            existing_main_subscription = await connection.fetchrow(
                "SELECT id FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                telegram_id, main_channel_id
            )

            action_type_for_history = ""
            main_subscription_record_id = None

            if existing_main_subscription:
                success_update = await update_subscription_db(
                    connection, telegram_id, main_channel_id, subscription_type_id,
                    calculated_new_expiry_date, calculated_start_date, True,  # is_active
                    None,  # subscription_plan_id
                    None,  # payment_id
                    main_invite_link,
                    admin_action_source
                )
                if not success_update:
                    logging.critical(
                        f"ADMIN: Failed to update subscription for {telegram_id} in channel {main_channel_id}")
                    return jsonify({"error": "Failed to update main subscription record."}), 500
                main_subscription_record_id = existing_main_subscription['id']
                action_type_for_history = 'ADMIN_RENEWAL'
            else:
                newly_created_main_sub_id = await add_subscription(
                    connection, telegram_id, main_channel_id, subscription_type_id,
                    calculated_start_date, calculated_new_expiry_date, True,  # is_active
                    None,  # subscription_plan_id
                    None,  # payment_id
                    main_invite_link,
                    admin_action_source,
                    returning_id=True
                )
                if not newly_created_main_sub_id:
                    logging.critical(
                        f"ADMIN: Failed to create subscription for {telegram_id} in channel {main_channel_id}")
                    return jsonify({"error": "Failed to create main subscription record."}), 500
                main_subscription_record_id = newly_created_main_sub_id
                action_type_for_history = 'ADMIN_NEW'

            logging.info(
                f"ADMIN: Main subscription {action_type_for_history} for user {telegram_id}, channel {main_channel_id}, expiry {calculated_new_expiry_date}")

            # 7. معالجة القنوات الفرعية (كما في الكود الذي قدمته)
            secondary_channel_links_to_send = []
            all_channels_for_type = await connection.fetch(
                "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC, channel_name",
                subscription_type_id
            )

            for channel_data in all_channels_for_type:
                current_channel_id_being_processed = int(channel_data["channel_id"])
                current_channel_name = channel_data["channel_name"] or f"Channel {current_channel_id_being_processed}"
                is_current_channel_main = channel_data["is_main"]

                if not is_current_channel_main:
                    invite_res = await generate_channel_invite_link(telegram_id, current_channel_id_being_processed,
                                                                    current_channel_name)
                    if invite_res["success"]:
                        secondary_channel_links_to_send.append(
                            f"▫️ قناة <a href='{invite_res['invite_link']}'>{current_channel_name}</a>"
                        )
                        # لا يوجد اشتراك منفصل للقنوات الفرعية في جدول subscriptions
                        # فقط جدولة مهمة الإزالة
                        await add_scheduled_task(
                            connection, "remove_user", telegram_id,
                            current_channel_id_being_processed, calculated_new_expiry_date,  # clean_up=True (الافتراضي)
                        )
                        logging.info(
                            f"ADMIN: Scheduled 'remove_user' for SECONDARY channel {current_channel_name} (ID: {current_channel_id_being_processed}) at {calculated_new_expiry_date.astimezone(LOCAL_TZ)}")
                    else:
                        logging.warning(
                            f"ADMIN: Failed to generate invite for secondary channel {current_channel_name} for user {telegram_id}. Skipping.")

            # 8. تسجيل في `subscription_history`
            # اسم الخطة سيكون عامًا لأننا لا نستخدم subscription_plan_id
            admin_subscription_plan_name = "اشتراك إداري"  # أو "Admin Subscription"

            extra_data_for_history = json.dumps({
                "full_name": actual_full_name,
                "username": actual_username,
                "added_by_admin": True,
                "days_added": days_to_add,
                "source": admin_action_source,
                "total_channels_in_bundle": len(all_channels_for_type),
                "secondary_links_generated_count": len(secondary_channel_links_to_send)
            })

            history_query = """
                INSERT INTO subscription_history (
                    subscription_id, invite_link, action_type, subscription_type_name, subscription_plan_name,
                    renewal_date, expiry_date, telegram_id, extra_data, payment_id, source
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id
            """
            history_record = await connection.fetchrow(
                history_query, main_subscription_record_id, main_invite_link,
                action_type_for_history, subscription_type_name, admin_subscription_plan_name,
                calculated_start_date, calculated_new_expiry_date, telegram_id,
                extra_data_for_history,
                None,  # payment_id is None
                admin_action_source
            )
            subscription_history_id = history_record["id"] if history_record else None
            if subscription_history_id:
                logging.info(f"ADMIN: Action logged in subscription_history ID: {subscription_history_id}")
            else:
                logging.error(f"ADMIN: Failed to log action in subscription_history for user {telegram_id}")

            # 9. إنشاء وإرسال إشعار للمستخدم
            final_expiry_date_local = calculated_new_expiry_date.astimezone(LOCAL_TZ)
            notification_title_key = 'تجديد' if action_type_for_history == 'ADMIN_RENEWAL' else 'تفعيل'
            notification_title = f"{notification_title_key} اشتراك (إدارة): {subscription_type_name}"

            num_accessible_channels = 1 + len(secondary_channel_links_to_send)  # القناة الرئيسية + الفرعية

            notification_message_text = (
                f"🎉 مرحبًا {greeting_name},\n\n"
                f"تم {notification_title_key.lower()} اشتراكك في \"{subscription_type_name}\" بواسطة الإدارة.\n"
                f"صالح حتى: {final_expiry_date_local.strftime('%Y-%m-%d %H:%M %Z')}.\n"
                f"🔗 للانضمام للقناة الرئيسية: {main_invite_link}\n"
                f"يمكنك الآن الوصول إلى {num_accessible_channels} قناة."
            )

            extra_notification_data = {
                "subscription_type": subscription_type_name,
                "subscription_history_id": subscription_history_id,
                "expiry_date_iso": calculated_new_expiry_date.isoformat(),
                "start_date_iso": calculated_start_date.isoformat(),
                "main_invite_link": main_invite_link,
                "secondary_links_sent_count": len(secondary_channel_links_to_send),
                "admin_initiated": True,
                "source": admin_action_source
            }

            await create_notification(
                connection=connection, notification_type="admin_subscription_update", title=notification_title,
                message=notification_message_text, extra_data=extra_notification_data,
                is_public=False, telegram_ids=[telegram_id]
            )

            # إرسال رسالة منفصلة بروابط القنوات الفرعية إذا وجدت
            if secondary_channel_links_to_send:
                secondary_links_message_text = (
                        f"📬 بالإضافة إلى القناة الرئيسية، يمكنك الانضمام إلى القنوات الفرعية التالية لاشتراك \"{subscription_type_name}\":\n\n" +
                        "\n".join(secondary_channel_links_to_send) +
                        "\n\n💡 هذه الروابط خاصة بك وصالحة لفترة محدودة. يرجى الانضمام في أقرب وقت."
                )
                await send_message_to_user(telegram_id, secondary_links_message_text)

            # 10. إرجاع استجابة ناجحة للأدمن
            formatted_response_message_html = (
                f"✅ تم {notification_title_key.lower()} اشتراك \"{subscription_type_name}\" بنجاح للمستخدم {telegram_id} (<code>{greeting_name}</code>).<br>"
                f"ينتهي في: {final_expiry_date_local.strftime('%Y-%m-%d %H:%M:%S %Z')}.<br>"

            )
            if secondary_channel_links_to_send:
                formatted_response_message_html += "<br>📬 تم إرسال روابط القنوات الفرعية إلى المستخدم."
            else:
                formatted_response_message_html += "<br>ℹ️ لا توجد قنوات فرعية مرتبطة بهذا النوع من الاشتراك."

            response_data = {
                "message": f"Subscription for user {telegram_id} in '{subscription_type_name}' has been {action_type_for_history.lower().replace('_admin', '').replace('_', ' ')}.",
                "telegram_id": telegram_id,
                "subscription_type_name": subscription_type_name,
                "new_expiry_date_formatted": final_expiry_date_local.strftime('%Y-%m-%d %H:%M:%S %Z'),
                "start_date_formatted": calculated_start_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z'),
                "main_invite_link": main_invite_link,
                "action_taken": action_type_for_history,
                "formatted_message_html": formatted_response_message_html,
                "secondary_channels_processed": len(secondary_channel_links_to_send)
            }

            return jsonify(response_data), 200  # 200 OK لأن العملية قد تكون تحديثاً أو إنشاء

    except Exception as e:
        logging.error(f"ADMIN: Critical error in /subscriptions (admin) endpoint: {str(e)}", exc_info=True)
        error_message = str(e) if IS_DEVELOPMENT else "Internal server error"
        return jsonify({"error": error_message}), 500


@admin_routes.route("/subscriptions/cancel", methods=["POST"])
@permission_required("user_subscriptions.cancel")
async def cancel_subscription_admin():
    try:
        data = await request.get_json(force=True)
        telegram_id = int(data.get("telegram_id", 0))
        subscription_type_id = int(data.get("subscription_type_id", 0))
        if not telegram_id or not subscription_type_id:
            return jsonify({"error": "telegram_id and subscription_type_id are required"}), 400

        db_pool = current_app.db_pool
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # 1. اعثر على الاشتراك النشط الأقل انتهاءً
                active = await conn.fetchrow(
                    """
                    SELECT id, channel_id, start_date, expiry_date, source
                    FROM subscriptions
                    WHERE telegram_id = $1
                      AND subscription_type_id = $2
                      AND is_active = TRUE
                    ORDER BY expiry_date ASC NULLS LAST, id ASC
                    LIMIT 1
                    """,
                    telegram_id, subscription_type_id
                )
                if not active:
                    return jsonify({
                        "message": f"No active subscription found for user {telegram_id} in type {subscription_type_id}"
                    }), 404

                sub_id = active["id"]
                main_channel = int(active["channel_id"])
                orig_start = active["start_date"]
                orig_expiry = active["expiry_date"]
                orig_source = active["source"] or ""

                # 2. إزالة المستخدم من القنوات
                channels = [main_channel]
                rows = await conn.fetch(
                    "SELECT channel_id FROM subscription_type_channels WHERE subscription_type_id = $1",
                    subscription_type_id
                )
                channels += [int(r["channel_id"]) for r in rows if int(r["channel_id"]) != main_channel]
                for ch in channels:
                    await remove_users_from_channel(telegram_id, ch)

                # 3. إلغاء الاشتراك في الـ DB باستخدام subscription_id فقط
                cancel_time = datetime.now(timezone.utc)
                reason = f"{orig_source}_admin_canceled" if orig_source else "admin_canceled"
                upd_id = await cancel_subscription_db(conn, sub_id, cancel_time, reason)

                if not upd_id:
                    return jsonify({
                        "error": "Failed to cancel subscription in DB; it may already be inactive."
                    }), 500

                # 4. حذف المهام المجدولة
                await delete_scheduled_tasks_for_subscription(conn, telegram_id, channels)

                # 5. تسجيل الـ history
                await conn.execute(
                    """
                    INSERT INTO subscription_history (
                        subscription_id, action_type, subscription_type_name,
                        subscription_plan_name, renewal_date, expiry_date,
                        telegram_id, extra_data
                    ) VALUES ($1, 'ADMIN_CANCEL', 
                              (SELECT name FROM subscription_types WHERE id = $2),
                              'إلغاء إداري', $3, $4, $5, $6)
                    """,
                    sub_id, subscription_type_id, orig_start, cancel_time, telegram_id,
                    json.dumps({
                        "channels_removed": channels,
                        "source": reason,
                    })
                )

                return jsonify({
                    "message": f"Subscription {sub_id} canceled successfully.",
                    "subscription_id": sub_id,
                    "telegram_id": telegram_id,
                    "channels": channels
                }), 200

    except Exception as e:
        logging.error(f"ADMIN CANCEL: unexpected error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/subscriptions/export", methods=["GET"])
@permission_required("user_subscriptions.cancel")
async def export_subscriptions():
    try:
        subscription_type_id = request.args.get("subscription_type_id")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        active = request.args.get("active")  # يُتوقع "all" أو "true" أو "false"

        # استخدام مجموعة الحقول الافتراضية
        allowed_fields = {
            "full_name": "u.full_name",
            "username": "u.username",
            "telegram_id": "s.telegram_id",
            "wallet_address": "u.wallet_address",
            "expiry_date": "s.expiry_date",
            "is_active": "s.is_active"
        }
        default_fields = ["full_name", "username", "telegram_id", "wallet_address", "expiry_date", "is_active"]
        select_clause = ", ".join([allowed_fields[f] for f in default_fields])

        query = f"""
            SELECT {select_clause}
            FROM subscriptions s
            LEFT JOIN users u ON s.user_id = u.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE 1=1
        """
        params = []

        if subscription_type_id:
            # تحويل subscription_type_id إلى int قبل الإضافة
            try:
                subscription_type_id = int(subscription_type_id)
            except ValueError:
                return jsonify({"error": "Invalid subscription_type_id format"}), 400
            query += f" AND s.subscription_type_id = ${len(params) + 1}"
            params.append(subscription_type_id)
        if start_date:
            query += f" AND s.start_date >= ${len(params) + 1}"
            params.append(start_date)
        if end_date:
            query += f" AND s.start_date <= ${len(params) + 1}"
            params.append(end_date)
        if active and active.lower() != "all":
            if active.lower() == "true":
                query += " AND s.is_active = true"
            elif active.lower() == "false":
                query += " AND s.is_active = false"

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *params)
        data = [dict(row) for row in rows]
        return jsonify(data)
    except Exception as e:
        logging.error("Error exporting subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/wallet", methods=["GET"])
@permission_required("system.manage_wallet")
async def get_wallet_address():
    async with current_app.db_pool.acquire() as connection:
        wallet = await connection.fetchrow("SELECT wallet_address, api_key FROM wallet ORDER BY id DESC LIMIT 1")
    if wallet:
        return jsonify({
            "wallet_address": wallet["wallet_address"],
            "api_key": wallet["api_key"]
        }), 200
    else:
        return jsonify({"wallet_address": "", "api_key": ""}), 200


@admin_routes.route("/wallet", methods=["POST"])
@permission_required("system.manage_wallet")
async def update_wallet_address():
    data = await request.get_json()
    wallet_address = data.get("wallet_address")
    api_key = data.get("api_key")

    if not wallet_address or not api_key:
        return jsonify({"error": "جميع الحقول مطلوبة"}), 400

    async with current_app.db_pool.acquire() as connection:
        wallet = await connection.fetchrow("SELECT id FROM wallet LIMIT 1")
        if wallet:
            await connection.execute(
                "UPDATE wallet SET wallet_address = $1, api_key = $2 WHERE id = $3",
                wallet_address, api_key, wallet["id"]
            )
        else:
            await connection.execute(
                "INSERT INTO wallet (wallet_address, api_key) VALUES ($1, $2)",
                wallet_address, api_key
            )
    return jsonify({"message": "تم تحديث البيانات بنجاح"}), 200


@admin_routes.route("/admin/reminder-settings", methods=["GET"])
@permission_required("system.manage_reminder_settings")
async def get_reminder_settings():
    """الحصول على إعدادات التذكير الحالية"""
    try:
        async with current_app.db_pool.acquire() as connection:
            settings = await connection.fetchrow(
                "SELECT first_reminder, second_reminder, first_reminder_message, second_reminder_message FROM reminder_settings LIMIT 1"
            )

            if not settings:
                return jsonify({"error": "الإعدادات غير موجودة"}), 404

            return jsonify({
                "first_reminder": settings["first_reminder"],
                "second_reminder": settings["second_reminder"],
                "first_reminder_message": settings["first_reminder_message"],
                "second_reminder_message": settings["second_reminder_message"]
            }), 200

    except Exception as e:
        logging.error(f"Error getting reminder settings: {str(e)}")
        return jsonify({"error": "حدث خطأ أثناء جلب الإعدادات"}), 500


@admin_routes.route("/admin/reminder-settings", methods=["PUT"])
@permission_required("system.manage_reminder_settings")
async def update_reminder_settings():
    """تحديث إعدادات التذكير باستخدام PUT"""
    try:
        data = await request.get_json()
        first_reminder = data.get("first_reminder")
        second_reminder = data.get("second_reminder")
        first_reminder_message = data.get("first_reminder_message")
        second_reminder_message = data.get("second_reminder_message")

        # التحقق من وجود جميع الحقول المطلوبة
        if first_reminder is None or second_reminder is None:
            logging.error("❌ بيانات ناقصة في الطلب")
            return jsonify({"error": "جميع الحقول مطلوبة (first_reminder, second_reminder)"}), 400

        # التحقق من صحة نوع البيانات
        try:
            first_reminder = int(first_reminder)
            second_reminder = int(second_reminder)
        except (ValueError, TypeError):
            logging.error(f"❌ قيم غير صحيحة: first={first_reminder}, second={second_reminder}")
            return jsonify({"error": "يجب أن تكون القيم أرقامًا صحيحة موجبة"}), 400

        # التحقق من القيم الموجبة
        if first_reminder <= 0 or second_reminder <= 0:
            logging.error(f"❌ قيم غير صالحة: first={first_reminder}, second={second_reminder}")
            return jsonify({"error": "يجب أن تكون القيم أكبر من الصفر"}), 400

        # التحقق من وجود رسائل التذكير
        if not first_reminder_message or not second_reminder_message:
            logging.error("❌ رسائل التذكير مفقودة")
            return jsonify({"error": "رسائل التذكير مطلوبة"}), 400

        async with current_app.db_pool.acquire() as connection:
            # الحصول على الإعدادات الحالية
            existing_settings = await connection.fetchrow(
                "SELECT id FROM reminder_settings LIMIT 1"
            )

            if existing_settings:
                # تحديث الإعدادات الحالية
                await connection.execute(
                    """UPDATE reminder_settings 
                    SET first_reminder = $1, second_reminder = $2, 
                    first_reminder_message = $3, second_reminder_message = $4
                    WHERE id = $5""",
                    first_reminder, second_reminder,
                    first_reminder_message, second_reminder_message,
                    existing_settings["id"]
                )
                action_type = "update"
                log_msg = f"🔄 تم تحديث إعدادات التذكير: {first_reminder}h, {second_reminder}h"
            else:
                # إضافة إعدادات جديدة إذا لم تكن موجودة
                await connection.execute(
                    """INSERT INTO reminder_settings 
                    (first_reminder, second_reminder, first_reminder_message, second_reminder_message) 
                    VALUES ($1, $2, $3, $4)""",
                    first_reminder, second_reminder, first_reminder_message, second_reminder_message
                )
                action_type = "create"
                log_msg = f"✅ تم إضافة إعدادات التذكير: {first_reminder}h, {second_reminder}h"

            logging.info(log_msg)
            return jsonify({
                "message": "تم حفظ الإعدادات بنجاح",
                "action": action_type,
                "first_reminder": first_reminder,
                "second_reminder": second_reminder,
                "first_reminder_message": first_reminder_message,
                "second_reminder_message": second_reminder_message
            }), 200

    except Exception as e:
        logging.error(f"❌ خطأ فادح في تحديث الإعدادات: {str(e)}", exc_info=True)
        return jsonify({
            "error": "حدث خطأ داخلي أثناء معالجة الطلب",
            "details": str(e)
        }), 500


@admin_routes.route("/terms-conditions", methods=["GET"])
@permission_required("subscription_types.read")
async def get_terms_conditions():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, terms_array, updated_at
                FROM terms_conditions
                ORDER BY updated_at DESC
                LIMIT 1;
            """
            result = await connection.fetchrow(query)

            if result:
                return jsonify({
                    "id": result["id"],
                    "terms_array": result["terms_array"],
                    "updated_at": result["updated_at"].isoformat() if result["updated_at"] else None
                }), 200
            else:
                return jsonify({"terms_array": []}), 200

    except Exception as e:
        logging.error("Error fetching terms and conditions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# Create or update terms and conditions
@admin_routes.route("/terms-conditions", methods=["POST"])
@permission_required("subscription_types.update")
async def update_terms_conditions():
    try:
        data = await request.get_json()
        terms_array = data.get("terms_array", [])

        # Validate input
        if not isinstance(terms_array, list):
            return jsonify({"error": "terms_array must be a list"}), 400

        import json
        # تحويل قائمة Python إلى سلسلة نصية بتنسيق JSON
        terms_json = json.dumps(terms_array)

        async with current_app.db_pool.acquire() as connection:
            query = """
                INSERT INTO terms_conditions (terms_array)
                VALUES ($1::jsonb)
                RETURNING id, terms_array, updated_at;
            """
            result = await connection.fetchrow(query, terms_json)

        return jsonify({
            "id": result["id"],
            "terms_array": result["terms_array"],
            "updated_at": result["updated_at"].isoformat() if result["updated_at"] else None
        }), 201

    except Exception as e:
        logging.error("Error updating terms and conditions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# Delete specific terms and conditions (optional)
@admin_routes.route("/terms-conditions/<int:terms_id>", methods=["DELETE"])
@permission_required("subscription_types.delete")
async def delete_terms_conditions(terms_id):
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                DELETE FROM terms_conditions 
                WHERE id = $1
                RETURNING id;
            """
            result = await connection.fetchrow(query, terms_id)

            if result:
                return jsonify({"message": "Terms and conditions deleted successfully"}), 200
            else:
                return jsonify({"error": "Terms and conditions not found"}), 404

    except Exception as e:
        logging.error("Error deleting terms and conditions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


AVAILABLE_EXPORT_FIELDS_MAP = {
    "id": {"db_col": "u.id", "header": "ID"},
    "telegram_id": {"db_col": "u.telegram_id", "header": "معرف تليجرام"},
    "username": {"db_col": "CONCAT('@', u.username)", "header": "اسم المستخدم"},
    "full_name": {"db_col": "u.full_name", "header": "الاسم الكامل"},
    "wallet_address": {"db_col": "u.wallet_address", "header": "عنوان المحفظة (EVM)"},
    "ton_wallet_address": {"db_col": "u.ton_wallet_address", "header": "عنوان محفظة TON"},
    "wallet_app": {"db_col": "u.wallet_app", "header": "تطبيق المحفظة"},
    "subscription_count": {
        "db_col": "(SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id)",
        "header": "إجمالي الاشتراكات"
    },
    "active_subscription_count": {
        "db_col": "(SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id AND s.is_active = true)",
        "header": "الاشتراكات النشطة"
    },
    "created_at": {"db_col": "u.created_at", "header": "تاريخ الإنشاء"},
    # يمكنك إضافة المزيد من الحقول هنا
    # "last_login": {"db_col": "u.last_login_at", "header": "آخر تسجيل دخول"},
}


@admin_routes.route("/users/export", methods=["POST"])
@permission_required("bot_users.export")
async def export_users_endpoint():
    try:
        data = await request.get_json()
        if data is None:  # تحقق إذا كانت البيانات JSON فارغة أو غير صالحة
            return jsonify({"error": "Invalid JSON payload"}), 400

        requested_field_keys = data.get('fields', [])  # قائمة بمفاتيح الحقول المطلوبة من الواجهة الأمامية
        user_type_filter = data.get('user_type',
                                    'all')  # 'all', 'active_subscribers', 'any_subscribers' (كانت 'with_subscription')
        search_term = data.get('search', "").strip()

        # تحديد الحقول التي سيتم الاستعلام عنها من قاعدة البيانات
        # والمفاتيح التي ستستخدم كـ alias في SQL (وهي نفسها مفاتيح requested_field_keys)
        fields_to_query_map = {}  # { 'alias_key': 'db_col_expression', ... }

        if not requested_field_keys:  # إذا لم يتم تحديد حقول، استخدم كل الحقول المتاحة
            for key, details in AVAILABLE_EXPORT_FIELDS_MAP.items():
                fields_to_query_map[key] = details["db_col"]
        else:
            for key in requested_field_keys:
                if key in AVAILABLE_EXPORT_FIELDS_MAP:
                    fields_to_query_map[key] = AVAILABLE_EXPORT_FIELDS_MAP[key]["db_col"]
                else:
                    logging.warning(f"Requested field '{key}' not available for export and will be ignored.")

        if not fields_to_query_map:
            return jsonify({"error": "No valid fields selected or available for export"}), 400

        # بناء جملة SELECT الديناميكية
        # استخدام الـ key كـ alias للعمود ليتم استخدامه لاحقاً في DataFrame
        select_clauses = [f'{db_expr} AS "{alias_key}"' for alias_key, db_expr in fields_to_query_map.items()]
        dynamic_select_sql = ", ".join(select_clauses)

        base_query_from = "FROM users u"

        # بناء شروط WHERE
        where_clauses = ["1=1"]
        query_params = []  # تم تغيير الاسم من where_params ليكون أوضح
        param_idx = 1  # لترقيم متغيرات SQL ($1, $2, ...)

        # فلتر نوع المستخدم
        if user_type_filter == "active_subscribers":  # استخدام نفس المفاتيح من الواجهة الأمامية
            where_clauses.append(
                "(SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id AND s.is_active = true) > 0")
        elif user_type_filter == "any_subscribers":  # تغيير 'with_subscription' إلى 'any_subscribers' ليكون أوضح
            where_clauses.append("(SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id) > 0")

        # فلتر البحث
        if search_term:
            search_pattern = f"%{search_term}%"
            # يتم إضافة الشروط إلى where_clauses وإضافة القيم إلى query_params
            # التأكد من أن ترقيم الـ placeholder ($) يتزايد بشكل صحيح
            search_conditions_parts = []

            search_conditions_parts.append(f"u.telegram_id::TEXT ILIKE ${param_idx}")
            query_params.append(search_pattern)
            param_idx += 1

            search_conditions_parts.append(f"u.full_name ILIKE ${param_idx}")
            query_params.append(search_pattern)
            param_idx += 1

            search_conditions_parts.append(f"u.username ILIKE ${param_idx}")
            query_params.append(search_pattern)
            param_idx += 1  # زيادة العداد بعد آخر استخدام

            where_clauses.append(f"({' OR '.join(search_conditions_parts)})")

        where_sql = " AND ".join(where_clauses)
        order_by_clause = "ORDER BY u.id DESC"  # أو أي ترتيب آخر تفضله

        final_query = f"SELECT {dynamic_select_sql} {base_query_from} WHERE {where_sql} {order_by_clause}"
        logging.debug(f"Export query: {final_query} with params: {query_params}")

        # جلب البيانات من قاعدة البيانات
        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(final_query, *query_params)
            # تحويل سجلات asyncpg مباشرة إلى قائمة من القواميس
            # المفاتيح في القواميس ستكون هي الـ alias_key التي حددناها
            users_data_list = [dict(row) for row in rows]

        if not users_data_list:
            # يمكنك إرجاع ملف Excel فارغ مع العناوين أو رسالة خطأ
            # هنا نرجع رسالة بأن لا يوجد بيانات
            return jsonify({"message": "No data found matching your criteria for export."}), 404

        # تحويل البيانات إلى DataFrame
        df = pd.DataFrame(users_data_list)

        # إعادة تسمية الأعمدة في DataFrame لتكون العناوين النهائية في ملف Excel
        # مفاتيح df.columns ستكون هي الـ alias_key من SQL
        excel_column_headers = {
            alias_key: AVAILABLE_EXPORT_FIELDS_MAP[alias_key]["header"]
            for alias_key in df.columns  # الأعمدة الموجودة فعليًا في الـ DataFrame
            if alias_key in AVAILABLE_EXPORT_FIELDS_MAP  # تأكد أن الـ alias_key معرف
        }
        df.rename(columns=excel_column_headers, inplace=True)

        # إنشاء buffer في الذاكرة لملف Excel
        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='Users Data', index=False)  # اسم الورقة

            workbook = writer.book
            worksheet = writer.sheets['Users Data']

            # تنسيق عناوين الأعمدة (Header)
            header_format = workbook.add_format({
                'bold': True,
                'text_wrap': True,
                'valign': 'top',
                'fg_color': '#D7E4BC',  # لون أخضر فاتح مختلف قليلاً
                'border': 1,
                'align': 'center'
            })

            # تطبيق التنسيق على صف العناوين
            for col_num, value in enumerate(df.columns.values):  # df.columns الآن تحتوي على العناوين النهائية
                worksheet.write(0, col_num, value, header_format)

            # تحديد عرض الأعمدة تلقائيًا بناءً على المحتوى
            for i, col_name_excel in enumerate(df.columns):  # col_name_excel هو العنوان النهائي
                # الحصول على اسم العمود الأصلي (alias_key) للوصول إلى بياناته في df قبل إعادة التسمية
                # أو ببساطة استخدم df[col_name_excel] إذا كانت إعادة التسمية قد حدثت
                # df[col_name_excel] هو العمود بالاسم الجديد (header)
                column_data = df[col_name_excel]
                max_len = max(
                    column_data.astype(str).map(len).max(),  # أطول قيمة في العمود
                    len(str(col_name_excel))  # طول اسم العمود نفسه
                )
                adjusted_width = (max_len + 2) * 1.2  # إضافة مساحة إضافية وتعديل بسيط
                worksheet.set_column(i, i, min(adjusted_width, 50))  # تحديد حد أقصى للعرض (مثلاً 50)

        excel_buffer.seek(0)  # الرجوع لبداية الـ buffer

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"users_export_{timestamp}.xlsx"

        return await send_file(
            excel_buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            attachment_filename=filename,  # في Quart، اسم البارامتر هو attachment_filename
            # cache_timeout=0 # Quart's send_file لا يأخذ cache_timeout بنفس الطريقة، يمكن تجاهله أو التعامل معه بطرق أخرى إذا لزم الأمر
        )

    except ValueError as ve:  # مثل خطأ في تحويل JSON
        logging.error(f"Value error in /users/export: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters", "details": str(ve)}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /users/export: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed", "details": str(pe)}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /users/export: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@admin_routes.route("/dashboard/stats", methods=["GET"])
@permission_required("dashboard.view_stats")
async def get_dashboard_stats():
    try:
        async with current_app.db_pool.acquire() as conn:
            # الإحصائيات الأساسية
            stats_query = """
                SELECT 
                    -- إجمالي الاشتراكات النشطة
                    (SELECT COUNT(*) FROM subscriptions WHERE is_active = true) as active_subscriptions,

                    -- إجمالي المدفوعات المكتملة
                    (SELECT COUNT(*) FROM payments WHERE status = 'completed') as completed_payments,

                    -- إجمالي الإيرادات من المدفوعات المكتملة بعملة USDT فقط
                    (SELECT COALESCE(SUM(amount_received), 0) FROM payments WHERE status = 'completed' AND currency = 'USDT') as total_revenue,

                    -- إجمالي عدد المستخدمين من جدول users
                    (SELECT COUNT(*) FROM users) as total_users,

                    -- عدد المستخدمين الجدد آخر 30 يومًا من جدول users
                    (SELECT COUNT(*) FROM users 
                     WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days') as new_users_last_30_days,

                    -- الاشتراكات التي تنتهي صلاحيتها خلال 7 أيام القادمة
                    (SELECT COUNT(*) FROM subscriptions 
                     WHERE is_active = true 
                     AND expiry_date BETWEEN CURRENT_TIMESTAMP AND CURRENT_TIMESTAMP + INTERVAL '7 days') as expiring_soon,

                    -- إجمالي المدفوعات غير المكتملة (فاشلة، ملغاة، دفع ناقص)
                    (SELECT COUNT(*) FROM payments 
                     WHERE status IN ('failed', 'canceled', 'underpaid')) as total_failed_payments
            """

            stats_row = await conn.fetchrow(stats_query)
            stats = dict(stats_row) if stats_row else {}

            # حساب نسبة النمو للمستخدمين الجدد (آخر 30 يومًا مقارنة بالـ 30 يومًا التي سبقتها)
            # المستخدمون الجدد في الفترة من (اليوم - 60 يومًا) إلى (اليوم - 30 يومًا)
            previous_30_days_new_users_query = """
                SELECT COUNT(*) as previous_period_count
                FROM users 
                WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '60 days'
                AND created_at < CURRENT_TIMESTAMP - INTERVAL '30 days'
            """

            previous_period_row = await conn.fetchrow(previous_30_days_new_users_query)
            previous_period_count = previous_period_row['previous_period_count'] if previous_period_row else 0

            current_new_users = stats.get('new_users_last_30_days', 0)
            growth_percentage = 0
            if previous_period_count > 0:
                growth_percentage = ((current_new_users - previous_period_count) / previous_period_count) * 100
            elif current_new_users > 0:  # إذا كان لا يوجد مستخدمون في الفترة السابقة ولكن يوجد في الحالية
                growth_percentage = 100
                # إذا كان current_new_users هو 0 و previous_period_count هو 0، فالنسبة 0

            stats['user_growth_percentage'] = round(growth_percentage, 1)

            return jsonify(stats)

    except Exception as e:
        current_app.logger.error(f"Error in dashboard stats: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@admin_routes.route("/dashboard/revenue_chart", methods=["GET"])
@permission_required("dashboard.view_revenue_chart")
async def get_revenue_chart():
    try:
        period = request.args.get("period", "7days")  # 7days, 30days, 6months

        async with current_app.db_pool.acquire() as conn:
            if period == "7days":
                query = """
                    SELECT 
                        DATE(created_at) as date,
                        COALESCE(SUM(amount_received), 0) as revenue
                    FROM payments 
                    WHERE status = 'completed' 
                    AND currency = 'USDT'
                    AND created_at >= CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY DATE(created_at)
                    ORDER BY date
                """
            elif period == "30days":
                query = """
                    SELECT 
                        DATE(created_at) as date,
                        COALESCE(SUM(amount_received), 0) as revenue
                    FROM payments 
                    WHERE status = 'completed' 
                    AND currency = 'USDT'
                    AND created_at >= CURRENT_DATE - INTERVAL '30 days'
                    GROUP BY DATE(created_at)
                    ORDER BY date
                """
            else:  # 6months
                query = """
                    SELECT 
                        DATE_TRUNC('month', created_at) as date,
                        COALESCE(SUM(amount_received), 0) as revenue
                    FROM payments 
                    WHERE status = 'completed' 
                    AND currency = 'USDT'
                    AND created_at >= CURRENT_DATE - INTERVAL '6 months'
                    GROUP BY DATE_TRUNC('month', created_at)
                    ORDER BY date
                """

            rows = await conn.fetch(query)
            chart_data = []

            for row in rows:
                chart_data.append({
                    "date": row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date']),
                    "revenue": float(row['revenue'])
                })

            return jsonify(chart_data)

    except Exception as e:
        logging.error(f"Error in revenue chart: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/dashboard/subscriptions_chart", methods=["GET"])
@permission_required("dashboard.view_subscriptions_chart")
async def get_subscriptions_chart():
    try:
        async with current_app.db_pool.acquire() as conn:
            # إحصائيات الاشتراكات حسب النوع
            query = """
                SELECT 
                    st.name as subscription_type,
                    COUNT(s.id) as count,
                    COUNT(CASE WHEN s.is_active = true THEN 1 END) as active_count
                FROM subscription_types st
                LEFT JOIN subscriptions s ON st.id = s.subscription_type_id
                WHERE st.is_active = true
                GROUP BY st.id, st.name
                ORDER BY count DESC
            """

            rows = await conn.fetch(query)
            chart_data = []

            for row in rows:
                chart_data.append({
                    "name": row['subscription_type'],
                    "total": row['count'],
                    "active": row['active_count']
                })

            return jsonify(chart_data)

    except Exception as e:
        logging.error(f"Error in subscriptions chart: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/dashboard/recent_activities", methods=["GET"])
@permission_required("dashboard.view_recent_activities")
async def get_recent_activities():
    try:
        limit = int(request.args.get("limit", 10))

        async with current_app.db_pool.acquire() as conn:
            query = """
                SELECT 
                    sh.action_type,
                    sh.changed_at,
                    sh.telegram_id,
                    sh.subscription_type_name,
                    sh.subscription_plan_name,
                    sh.extra_data
                FROM subscription_history sh
                ORDER BY sh.changed_at DESC
                LIMIT $1
            """

            rows = await conn.fetch(query, limit)
            activities = []

            for row in rows:
                activity = {
                    "action_type": row['action_type'],
                    "changed_at": row['changed_at'].isoformat() if row['changed_at'] else None,
                    "telegram_id": row['telegram_id'],
                    "subscription_type": row['subscription_type_name'],
                    "subscription_plan": row['subscription_plan_name'],
                    "extra_data": row['extra_data']
                }
                activities.append(activity)

            return jsonify(activities)

    except Exception as e:
        logging.error(f"Error in recent activities: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/dashboard/recent_payments", methods=["GET"])
@permission_required("dashboard.view_recent_payments")
async def get_recent_payments():
    try:
        limit = int(request.args.get("limit", 10))

        async with current_app.db_pool.acquire() as conn:
            query = """
                SELECT 
                    p.id,
                    p.amount,
                    p.currency,
                    p.status,
                    p.created_at,
                    p.username,
                    p.full_name,
                    sp.name as plan_name
                FROM payments p
                LEFT JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
                ORDER BY p.created_at DESC
                LIMIT $1
            """

            rows = await conn.fetch(query, limit)
            payments = []

            for row in rows:
                payment = {
                    "id": row['id'],
                    "amount": float(row['amount']),
                    "currency": row['currency'],
                    "status": row['status'],
                    "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                    "username": row['username'],
                    "full_name": row['full_name'],
                    "plan_name": row['plan_name']
                }
                payments.append(payment)

            return jsonify(payments)

    except Exception as e:
        logging.error(f"Error in recent payments: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500