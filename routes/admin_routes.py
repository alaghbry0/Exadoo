import os
import json
import logging
from quart import Blueprint, request, jsonify, abort, current_app
from config import DATABASE_CONFIG, SECRET_KEY
from auth import get_current_user
from datetime import datetime
import pytz
from functools import wraps
import jwt
import asyncpg
import asyncio
from utils.db_utils import remove_users_from_channel


# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# إنشاء Blueprint مع بادئة URL
admin_routes = Blueprint("admin_routes", __name__, url_prefix="/api/admin")


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
@role_required("owner")
async def get_users_panel():
    """جلب قائمة المستخدمين من جدول panel_users"""
    async with current_app.db_pool.acquire() as connection:
        users = await connection.fetch("SELECT email, display_name, role FROM panel_users")
        users_list = [dict(user) for user in users]
    return jsonify({"users": users_list}), 200


@admin_routes.route("/add_owner", methods=["POST"])
@role_required("owner")
async def add_owner():
    """إضافة مالك جديد (Owner)"""
    data = await request.get_json()
    email = data.get("email")
    display_name = data.get("display_name", "")

    if not email:
        return jsonify({"error": "Email is required"}), 400

    async with current_app.db_pool.acquire() as connection:
        # استخدام نفس الجدول panel_users
        existing_user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)

        if existing_user:
            return jsonify({"error": "User already exists"}), 400

        await connection.execute(
            "INSERT INTO panel_users (email, display_name, role) VALUES ($1, $2, 'owner')",
            email, display_name
        )

    return jsonify({"message": "Owner added successfully"}), 201


@admin_routes.route("/add_admin", methods=["POST"])
@role_required("owner")
async def add_admin():
    """إضافة مسؤول جديد (Admin)"""
    data = await request.get_json()
    email = data.get("email")
    display_name = data.get("display_name", "")

    if not email:
        return jsonify({"error": "Email is required"}), 400

    async with current_app.db_pool.acquire() as connection:
        # استخدام نفس الجدول panel_users
        existing_user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)

        if existing_user:
            return jsonify({"error": "User already exists"}), 400

        await connection.execute(
            "INSERT INTO panel_users (email, display_name, role) VALUES ($1, $2, 'admin')",
            email, display_name
        )

    return jsonify({"message": "Admin added successfully"}), 201


@admin_routes.route("/remove_user", methods=["DELETE"])
@role_required("owner")
async def remove_user():
    """حذف حساب موجود (Owner أو Admin)"""
    data = await request.get_json()
    email = data.get("email")

    if not email:
        return jsonify({"error": "Email is required"}), 400

    async with current_app.db_pool.acquire() as connection:
        # التحقق مما إذا كان المستخدم موجودًا
        existing_user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)

        if not existing_user:
            return jsonify({"error": "User not found"}), 404

        # التأكد من عدم حذف آخر Owner في النظام
        if existing_user["role"] == "owner":
            owners_count = await connection.fetchval("SELECT COUNT(*) FROM panel_users WHERE role = 'owner'")
            if owners_count <= 1:
                return jsonify({"error": "Cannot delete the last owner"}), 403

        # تنفيذ الحذف
        await connection.execute("DELETE FROM panel_users WHERE email = $1", email)

    return jsonify({"message": f"User {email} removed successfully"}), 200


# ضف هذه الدوال إلى ملف الـ admin_routes في الخادم الخلفي

# في ملف admin_routes.py
@admin_routes.route("/users", methods=["GET"])
@role_required("admin") # تأكد من تفعيل هذا الديكوريتور
async def get_users_endpoint(): # تم تغيير الاسم
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
            "total": total_records, # تم التغيير من total_count
            "page": page,
            "page_size": page_size,
            "users_count": users_stat_count # إحصائية بسيطة
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
@role_required("admin")
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
                SELECT COALESCE(SUM(amount), 0) as total_payments,
                       COUNT(*) as payment_count
                FROM payments
                WHERE telegram_id = $1 AND status = 'completed'
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
# @role_required("admin") # افترض أن هذا الديكوريتور موجود
async def create_subscription_type():
    try:
        data = await request.get_json()
        name = data.get("name")
        main_channel_id = data.get("main_channel_id")  # تم تغيير الاسم ليعكس الغرض
        description = data.get("description", "")
        image_url = data.get("image_url", "")
        features = data.get("features", [])
        usp = data.get("usp", "")
        is_active = data.get("is_active", True)
        # قائمة اختيارية للقنوات الفرعية
        # كل عنصر في القائمة يجب أن يكون قاموسًا مثل: {"channel_id": 123, "channel_name": "اسم القناة"}
        secondary_channels_data = data.get("secondary_channels", [])  # قائمة اختيارية

        if not name or main_channel_id is None:
            return jsonify({"error": "Missing required fields: name and main_channel_id"}), 400

        try:
            # التحقق من أن main_channel_id هو رقم صحيح
            main_channel_id = int(main_channel_id)
        except ValueError:
            return jsonify({"error": "main_channel_id must be an integer"}), 400

        # التحقق من صحة بيانات القنوات الفرعية
        valid_secondary_channels = []
        if not isinstance(secondary_channels_data, list):
            return jsonify({"error": "secondary_channels must be a list"}), 400

        for ch_data in secondary_channels_data:
            if not isinstance(ch_data, dict) or "channel_id" not in ch_data:
                return jsonify({"error": "Each secondary channel must be an object with a 'channel_id'"}), 400
            try:
                ch_id = int(ch_data["channel_id"])
                ch_name = ch_data.get("channel_name")  # اسم القناة اختياري هنا
                if ch_id == main_channel_id:  # لا يمكن أن تكون القناة الفرعية هي نفسها الرئيسية
                    return jsonify(
                        {"error": f"Secondary channel ID {ch_id} cannot be the same as the main channel ID."}), 400
                valid_secondary_channels.append({"channel_id": ch_id, "channel_name": ch_name})
            except ValueError:
                return jsonify({
                    "error": f"Invalid channel_id '{ch_data['channel_id']}' in secondary_channels. Must be an integer."}), 400

        async with current_app.db_pool.acquire() as connection:
            async with connection.transaction():  # استخدام Transaction لضمان سلامة البيانات
                # 1. إدراج في subscription_types
                query_type = """
                    INSERT INTO subscription_types
                    (name, channel_id, description, image_url, features, usp, is_active)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                    RETURNING id, name, channel_id AS main_channel_id, description, image_url, features, usp, is_active, created_at;
                """
                created_type = await connection.fetchrow(
                    query_type, name, main_channel_id, description, image_url,
                    json.dumps(features), usp, is_active
                )
                if not created_type:
                    # هذا لا ينبغي أن يحدث إذا كان الاستعلام صحيحًا ولم يكن هناك خطأ في قاعدة البيانات
                    raise Exception("Failed to create subscription type record.")

                new_type_id = created_type["id"]

                # 2. إدراج القناة الرئيسية في subscription_type_channels
                # افترض أن لديك اسمًا للقناة الرئيسية، أو يمكنك جعله اختياريًا أو جلبه لاحقًا
                main_channel_name_from_data = data.get("main_channel_name", f"Main Channel for {name}")  # اسم افتراضي

                await connection.execute(
                    """
                    INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                    channel_name = EXCLUDED.channel_name, is_main = TRUE;
                    """,
                    new_type_id, main_channel_id, main_channel_name_from_data
                )

                # 3. إدراج القنوات الفرعية في subscription_type_channels
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

                # جلب كل القنوات المرتبطة بعد الإدراج
                linked_channels_query = "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1"
                linked_channels_rows = await connection.fetch(linked_channels_query, new_type_id)

                response_data = dict(created_type)
                response_data["linked_channels"] = [dict(row) for row in linked_channels_rows]

        return jsonify(response_data), 201

    except Exception as e:
        logging.error("Error creating subscription type: %s", e, exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


# --- تعديل بيانات نوع اشتراك موجود ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["PUT"])
# @role_required("admin")
async def update_subscription_type(type_id: int):
    try:
        data = await request.get_json()

        # الحقول التي يمكن تحديثها في subscription_types
        name = data.get("name")
        new_main_channel_id_str = data.get("main_channel_id")
        description = data.get("description")
        image_url = data.get("image_url")
        features = data.get("features")  # قائمة
        usp = data.get("usp")
        is_active = data.get("is_active")
        main_channel_name_from_data = data.get("main_channel_name")  # اسم القناة الرئيسية المحدث

        # قائمة القنوات الفرعية (إذا تم تمريرها، ستحل محل القائمة القديمة بالكامل)
        # إذا لم يتم تمريرها (secondary_channels is None)، لا نغير القنوات الفرعية الحالية.
        # إذا تم تمريرها كقائمة فارغة ([]), سيتم حذف كل القنوات الفرعية.
        secondary_channels_data = data.get("secondary_channels")  # يمكن أن يكون None, أو قائمة

        new_main_channel_id = None
        if new_main_channel_id_str is not None:
            try:
                new_main_channel_id = int(new_main_channel_id_str)
            except ValueError:
                return jsonify({"error": "main_channel_id must be an integer if provided"}), 400

        valid_new_secondary_channels = []
        if secondary_channels_data is not None:  # فقط إذا تم توفير المفتاح
            if not isinstance(secondary_channels_data, list):
                return jsonify({"error": "secondary_channels must be a list if provided"}), 400
            for ch_data in secondary_channels_data:
                if not isinstance(ch_data, dict) or "channel_id" not in ch_data:
                    return jsonify({"error": "Each secondary channel must be an object with 'channel_id'"}), 400
                try:
                    ch_id = int(ch_data["channel_id"])
                    if new_main_channel_id is not None and ch_id == new_main_channel_id:  # التحقق مقابل الـ ID الجديد إذا تم توفيره
                        return jsonify({
                            "error": f"Secondary channel ID {ch_id} cannot be the same as the new main channel ID."}), 400
                    # إذا لم يتم توفير new_main_channel_id، يجب التحقق مقابل الـ ID الرئيسي الحالي من قاعدة البيانات (خطوة إضافية)

                    valid_new_secondary_channels.append({
                        "channel_id": ch_id,
                        "channel_name": ch_data.get("channel_name")
                    })
                except ValueError:
                    return jsonify(
                        {"error": f"Invalid channel_id '{ch_data['channel_id']}' in secondary_channels."}), 400

        async with current_app.db_pool.acquire() as connection:
            async with connection.transaction():
                # 1. جلب القناة الرئيسية الحالية (إذا لم يتم توفير قناة رئيسية جديدة)
                current_main_channel_id_db = new_main_channel_id  # استخدام الجديد إذا توفر
                if current_main_channel_id_db is None:  # إذا لم يتم توفير main_channel_id جديد في الطلب
                    current_main_channel_id_db = await connection.fetchval(
                        "SELECT channel_id FROM subscription_types WHERE id = $1", type_id)
                    if current_main_channel_id_db is None:  # نوع الاشتراك غير موجود
                        return jsonify({"error": "Subscription type not found"}), 404

                # التأكد من أن القنوات الفرعية الجديدة لا تتعارض مع القناة الرئيسية النهائية
                for sec_ch in valid_new_secondary_channels:
                    if sec_ch["channel_id"] == current_main_channel_id_db:
                        return jsonify({
                            "error": f"Secondary channel ID {sec_ch['channel_id']} conflicts with the effective main channel ID."}), 400

                # 2. تحديث subscription_types
                query_type_update = """
                    UPDATE subscription_types
                    SET name = COALESCE($1, name),
                        channel_id = COALESCE($2, channel_id),
                        description = COALESCE($3, description),
                        image_url = COALESCE($4, image_url),
                        features = COALESCE($5::jsonb, features),
                        usp = COALESCE($6, usp),
                        is_active = COALESCE($7, is_active)
                    WHERE id = $8
                    RETURNING id, name, channel_id AS main_channel_id, description, image_url, features, usp, is_active, created_at;
                """
                features_json = json.dumps(features) if features is not None else None
                updated_type = await connection.fetchrow(
                    query_type_update, name, new_main_channel_id, description, image_url,
                    features_json, usp, is_active, type_id
                )

                if not updated_type:
                    return jsonify({"error": "Subscription type not found or no update occurred"}), 404

                effective_main_channel_id = updated_type["main_channel_id"]  # القناة الرئيسية بعد التحديث

                # 3. إدارة القنوات في subscription_type_channels
                # إذا تم توفير secondary_channels_data (حتى لو قائمة فارغة), سنقوم بإعادة بناء الروابط.
                # إذا كان secondary_channels_data هو None, لا نلمس الروابط الحالية إلا لتحديث is_main إذا تغيرت القناة الرئيسية.

                # أ. تحديث أو إضافة القناة الرئيسية الجديدة
                if main_channel_name_from_data is None:  # إذا لم يتم توفير اسم جديد للقناة الرئيسية
                    # جلب الاسم الحالي للقناة الرئيسية إذا كانت موجودة، أو استخدم اسم افتراضي
                    existing_main_ch_name_row = await connection.fetchrow(
                        "SELECT channel_name FROM subscription_type_channels WHERE subscription_type_id = $1 AND channel_id = $2",
                        type_id, effective_main_channel_id
                    )
                    main_channel_name_to_use = existing_main_ch_name_row[
                        'channel_name'] if existing_main_ch_name_row and existing_main_ch_name_row[
                        'channel_name'] else f"Main Channel for {updated_type['name']}"
                else:
                    main_channel_name_to_use = main_channel_name_from_data

                # أولاً، تأكد من أن جميع القنوات الأخرى ليست هي الرئيسية
                await connection.execute(
                    "UPDATE subscription_type_channels SET is_main = FALSE WHERE subscription_type_id = $1 AND channel_id != $2",
                    type_id, effective_main_channel_id
                )
                # ثم، قم بتعيين/تحديث القناة الرئيسية الفعلية
                await connection.execute(
                    """
                    INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                    channel_name = EXCLUDED.channel_name, is_main = TRUE;
                    """,
                    type_id, effective_main_channel_id, main_channel_name_to_use
                )

                # ب. إذا تم توفير قائمة بالقنوات الفرعية (حتى لو فارغة)، قم بإعادة بنائها
                if secondary_channels_data is not None:
                    # حذف جميع القنوات الفرعية القديمة (التي ليست هي القناة الرئيسية الجديدة)
                    await connection.execute(
                        "DELETE FROM subscription_type_channels WHERE subscription_type_id = $1 AND is_main = FALSE AND channel_id != $2",
                        type_id, effective_main_channel_id
                        # لا تحذف القناة الرئيسية إذا كانت بالخطأ is_main=false مؤقتاً
                    )
                    # تأكد من أن is_main=false لا تحذف القناة الرئيسية
                    await connection.execute(
                        "DELETE FROM subscription_type_channels WHERE subscription_type_id = $1 AND channel_id != $2 AND is_main = FALSE",
                        type_id, effective_main_channel_id
                    )

                    # إضافة القنوات الفرعية الجديدة
                    for sec_channel in valid_new_secondary_channels:
                        await connection.execute(
                            """
                            INSERT INTO subscription_type_channels (subscription_type_id, channel_id, channel_name, is_main)
                            VALUES ($1, $2, $3, FALSE)
                            ON CONFLICT (subscription_type_id, channel_id) DO UPDATE SET
                            channel_name = EXCLUDED.channel_name, is_main = FALSE; 
                            """,  # تأكد من أن التحديث لا يجعلها is_main = TRUE بالخطأ
                            type_id, sec_channel["channel_id"], sec_channel.get("channel_name")
                        )

                # جلب كل القنوات المرتبطة بعد التحديث
                linked_channels_query = "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC, channel_name"
                linked_channels_rows = await connection.fetch(linked_channels_query, type_id)

                response_data = dict(updated_type)
                response_data["linked_channels"] = [dict(row) for row in linked_channels_rows]

        return jsonify(response_data), 200

    except Exception as e:
        logging.error("Error updating subscription type %s: %s", type_id, e, exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


# --- حذف نوع اشتراك ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["DELETE"])
# @role_required("admin")
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
# @role_required("owner") # أو "admin"
async def get_subscription_types():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    st.id, st.name, st.channel_id AS main_channel_id, st.description, 
                    st.image_url, st.features, st.usp, st.is_active, st.created_at,
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
            # features بالفعل jsonb، لا حاجة لـ json.loads إذا كان PostgreSQL يُرجعها كـ dict/list
            # إذا كانت تُرجع كنص JSON، عندها ستحتاج للتحويل.
            # linked_channels يُرجعها json_agg كـ JSON (غالبًا نص)، لذا قد تحتاج للتحويل
            if isinstance(type_item.get("linked_channels"), str):
                type_item["linked_channels"] = json.loads(type_item["linked_channels"]) if type_item[
                    "linked_channels"] else []
            elif type_item.get("linked_channels") is None:  # إذا لم تكن هناك قنوات مرتبطة، json_agg قد يُرجع NULL
                type_item["linked_channels"] = []

            types_list.append(type_item)

        return jsonify(types_list), 200
    except Exception as e:
        logging.error("Error fetching subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# --- جلب تفاصيل نوع اشتراك معين ---
@admin_routes.route("/subscription-types/<int:type_id>", methods=["GET"])
# @role_required("admin")
async def get_subscription_type(type_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            query_type = """
                SELECT id, name, channel_id AS main_channel_id, description, image_url, features, usp, is_active, created_at
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
            response_data["linked_channels"] = [dict(row) for row in linked_channels_rows]

        return jsonify(response_data), 200
    except Exception as e:
        logging.error("Error fetching subscription type %s: %s", type_id, e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


#######################################
# نقاط API لإدارة خطط الاشتراك (subscription_plans)
#######################################

@admin_routes.route("/subscription-plans", methods=["POST"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
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
@role_required("admin")  # ✅ استخدام @role_required("admin")
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


# جلب جميع خطط الاشتراك، مع إمكانية التصفية حسب subscription_type_id
@admin_routes.route("/subscription-plans", methods=["GET"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
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
@role_required("admin")  # ✅ استخدام @role_required("admin")
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
@role_required("admin")
async def get_subscriptions_endpoint():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        # الفلاتر من الواجهة الأمامية الجديدة
        status_filter = request.args.get("status")  # "active", "inactive", "all"
        type_filter_id_str = request.args.get("subscription_type_id") # سيكون ID أو "all"
        source_filter = request.args.get("source") # سيكون قيمة المصدر أو "all"
        start_date_filter = request.args.get("start_date") # لـ expiry_date (YYYY-MM-DD)
        end_date_filter = request.args.get("end_date")   # لـ expiry_date (YYYY-MM-DD)
        
        # الفرز: للتوحيد مع PaymentsPage، سنقوم بالفرز الافتراضي حاليًا.
        # إذا أردت دعم الفرز من جانب الخادم لاحقًا، ستحتاج لتمرير sortModel من DataTable.
        order_by_clause = "ORDER BY s.id DESC" # أو s.created_at DESC إذا كان لديك ومناسبًا

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
            is_active_bool = status_filter.lower() == "active" # أو true إذا كانت الواجهة ترسلها
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
                where_params.append(source_filter) # يمكنك إضافة % إذا أردت بحث جزئي: f"%{source_filter}%"

        # 4. فلاتر تاريخ الانتهاء (expiry_date)
        if start_date_filter and start_date_filter.strip():
            where_clauses.append(f"s.expiry_date >= ${len(where_params) + 1}::DATE")
            where_params.append(start_date_filter)

        if end_date_filter and end_date_filter.strip():
            where_clauses.append(f"s.expiry_date < (${len(where_params) + 1}::DATE + INTERVAL '1 day')") # غير شامل لليوم التالي
            where_params.append(end_date_filter)

        # 5. فلتر البحث (Search term)
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = []
            # يجب أن تبدأ معاملات البحث من حيث انتهت where_params
            current_param_idx = len(where_params)

            # البحث في telegram_id كـ TEXT
            search_conditions.append(f"s.telegram_id::TEXT ILIKE ${current_param_idx + 1}")
            where_params.append(search_pattern) # أضف المعامل إلى where_params
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
        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0] #
        
        # --- استعلام البيانات الرئيسي ---
        query_final_params = list(where_params) # انسخ where_params لإنشاء query_final_params
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
                param_counter_for_stat +=1
            except ValueError: pass
        
        if source_filter and source_filter.strip().lower() != "all":
            if source_filter.strip().lower() == "none" or source_filter.strip().lower() == "null":
                 stat_where_clauses.append(f"(s.source IS NULL OR s.source = '')")
            else:
                stat_where_clauses.append(f"s.source ILIKE ${param_counter_for_stat}")
                stat_where_params.append(source_filter)
                param_counter_for_stat +=1

        if start_date_filter and start_date_filter.strip():
            stat_where_clauses.append(f"s.expiry_date >= ${param_counter_for_stat}::DATE")
            stat_where_params.append(start_date_filter)
            param_counter_for_stat +=1
        
        if end_date_filter and end_date_filter.strip():
            stat_where_clauses.append(f"s.expiry_date < (${param_counter_for_stat}::DATE + INTERVAL '1 day')")
            stat_where_params.append(end_date_filter)
            param_counter_for_stat +=1

        if search_term:
            search_pattern_stat = f"%{search_term}%" # استخدام نفس النمط
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
        if not stat_where_clauses: # يعني فقط s.is_active = $1
            active_subscriptions_stat_where_sql = f"s.is_active = $1" # المعامل هو True

        # تأكد من أن active_subscriptions_stat_where_sql ليست فارغة إذا لم تكن هناك أي شروط أخرى غير is_active
        if not active_subscriptions_stat_where_sql.strip() and True in stat_where_params : # فقط is_active = true
             active_subscriptions_stat_where_sql = f"s.is_active = $1"

        active_subscriptions_stat_query = f"""
            SELECT COUNT(s.id) as active_total
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
            WHERE {active_subscriptions_stat_where_sql if active_subscriptions_stat_where_sql.strip() else 's.is_active = TRUE'} 
        """ # استخدام TRUE إذا كان where_sql فارغًا لضمان جلب جميع النشطين
        # إذا كان stat_where_params فارغًا بسبب عدم وجود فلاتر، فإن active_subscriptions_stat_where_sql سيكون فارغًا أيضًا.
        # في هذه الحالة، نريد عد جميع الاشتراكات النشطة.
        # إذا كان active_subscriptions_stat_where_sql فارغًا، و stat_where_params فارغًا أيضًا، هذا يعني لا فلاتر أخرى، فقط is_active=true
        if not active_subscriptions_stat_where_sql.strip() and not stat_where_params:
            active_subscriptions_stat_query = active_subscriptions_stat_query.replace("WHERE  AND", "WHERE") # تنظيف بسيط
            # لا يمكن أن يكون active_subscriptions_stat_where_sql فارغًا إذا كان stat_where_params يحتوي على True
            # الحالة الوحيدة التي يكون فيها فارغًا هي عدم وجود أي فلاتر أخرى + لم يتم إضافة is_active = true بعد
            # لكن الكود أعلاه يضيف is_active = true دائمًا.
            # الأمان:
            if not stat_where_params and "WHERE " == active_subscriptions_stat_query.strip()[-6:]: # إذا انتهى بـ WHERE وفراغ
                 active_subscriptions_stat_query = active_subscriptions_stat_query.replace("WHERE ", "WHERE s.is_active = TRUE ")

        items_data = []
        total_records = 0
        active_subscriptions_total_stat = 0

        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(query, *query_final_params)
            items_data = [dict(row) for row in rows]
            
            count_row = await conn.fetchrow(count_query, *where_params)
            total_records = count_row['total'] if count_row and count_row['total'] is not None else 0
            
            active_stat_row = await conn.fetchrow(active_subscriptions_stat_query, *stat_where_params)
            active_subscriptions_total_stat = active_stat_row['active_total'] if active_stat_row and active_stat_row['active_total'] is not None else 0

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
@role_required("admin")
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
@role_required("admin")
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
@role_required("admin")
async def get_pending_subscriptions_endpoint(): # تم تغيير الاسم ليناسب endpoint
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()
        
        # الفلاتر الجديدة
        status_filter = request.args.get("status", "all").lower() # "pending", "complete", "all"
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
@role_required("admin")
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
@role_required("admin")
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
@role_required("admin")
async def get_legacy_subscriptions_endpoint(): # تم تغيير الاسم
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        # الفلاتر الجديدة
        processed_filter_str = request.args.get("processed") # "true", "false", "all"
        # يمكنك إضافة فلاتر تاريخ لـ expiry_date إذا لزم الأمر
        # start_date_filter = request.args.get("start_date")
        # end_date_filter = request.args.get("end_date")

        # الفرز: للتوحيد، نستخدم فرزًا افتراضيًا.
        order_by_clause = "ORDER BY ls.expiry_date DESC" # أو ls.id DESC

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
            processed_legacy_total_stat = processed_stat_row['processed_total'] if processed_stat_row and processed_stat_row['processed_total'] is not None else 0

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
@role_required("admin")
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
@role_required("admin")
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
            where_params.append(search_pattern) # أضف المعامل إلى where_params
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
            rows = [dict(row) for row in rows_result] # تحويل السجلات إلى قواميس هنا
            
            # تنفيذ استعلام العدد
            count_result_row = await connection.fetchrow(count_query, *where_params) # استخدم where_params هنا
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
            if status_filter and status_filter.lower() != "all": # استخدام status_filter من بداية الدالة
                rev_query_clauses.append(f"status = ${len(rev_params) + 1}")
                rev_params.append(status_filter)
            
            # يمكنك إضافة فلاتر user_id و dates هنا بنفس الطريقة إذا كانت مطلوبة للإيرادات
            if user_id_filter_str and user_id_filter_str.strip():
                try:
                    user_id_val_rev = int(user_id_filter_str)
                    rev_query_clauses.append(f"user_id = ${len(rev_params) + 1}")
                    rev_params.append(user_id_val_rev)
                except ValueError:
                    pass # تجاهل إذا كان غير صالح لهذا الاستعلام المحدد

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
            "data": rows, # rows تم تحويلها إلى قواميس بالفعل
            "total": total_records,
            "page": page,
            "page_size": page_size,
            "completed_count": completed_payments_count,
            "total_revenue": float(total_revenue) if total_revenue else 0.0 # تأكد من أن الإيرادات رقم عشري
        }
        return jsonify(response)

    except ValueError as ve: # لالتقاط أخطاء تحويل page/page_size إلى int
        logging.error(f"Invalid parameter format: {ve}", exc_info=True)
        return jsonify({"error": "Invalid parameter format", "details": str(ve)}), 400
    except Exception as e:
        logging.error("Error fetching payments: %s", e, exc_info=True)
        # في الإنتاج، قد لا ترغب في إرسال تفاصيل الخطأ للعميل
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@admin_routes.route("/incoming-transactions", methods=["GET"])
@role_required("admin")
async def get_incoming_transactions():
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        
        # استخراج الفلاتر من الطلب
        processed_filter_str = request.args.get("processed") # سيكون "true", "false", أو "all"
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

        where_clauses = ["1=1"]
        where_params = [] 
        
        # 1. فلتر الحالة (Processed)
        if processed_filter_str and processed_filter_str.lower() != "all":
            try:
                # تحويل "true" أو "false" إلى قيمة boolean
                processed_val = processed_filter_str.lower() == "true"
                where_clauses.append(f"it.processed = ${len(where_params) + 1}")
                where_params.append(processed_val)
            except ValueError:
                logging.warning(f"Invalid processed filter value: {processed_filter_str}")
        # إذا لم يتم تحديد فلتر (أو كان 'all')، لا نضيف شيئاً لـ where_clauses بخصوص processed

        # 2. فلاتر التاريخ (Date filters on received_at)
        if start_date_filter and start_date_filter.strip():
            where_clauses.append(f"it.received_at >= ${len(where_params) + 1}::TIMESTAMP")
            where_params.append(start_date_filter)

        if end_date_filter and end_date_filter.strip():
            where_clauses.append(f"it.received_at <= ${len(where_params) + 1}::TIMESTAMP")
            where_params.append(end_date_filter)

        # 3. فلتر البحث (Search term)
        if search_term:
            search_pattern = f"%{search_term}%"
            search_conditions = []
            current_param_index = len(where_params)

            search_conditions.append(f"it.txhash ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            current_param_index += 1

            search_conditions.append(f"it.sender_address ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            current_param_index += 1
            
            search_conditions.append(f"it.memo ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            current_param_index += 1

            search_conditions.append(f"it.payment_token ILIKE ${current_param_index + 1}")
            where_params.append(search_pattern)
            # current_param_index += 1 # لا حاجة لزيادته هنا

            where_clauses.append(f"({' OR '.join(search_conditions)})")

        # --- بناء جملة WHERE النهائية ---
        where_sql = " AND ".join(where_clauses) if len(where_clauses) > 1 else where_clauses[0]

        # --- استعلام البيانات الرئيسي ---
        query_final_params = list(where_params) 
        query = f"""
            {base_query_select}
            WHERE {where_sql}
            ORDER BY it.received_at DESC 
            LIMIT ${len(query_final_params) + 1} OFFSET ${len(query_final_params) + 2}
        """
        query_final_params.append(page_size)
        query_final_params.append(offset)

        # --- استعلام العد ---
        count_query = f"""
            {count_base_query_select}
            WHERE {where_sql}
        """
        
        rows_data = []
        total_records = 0
        processed_transactions_count = 0 # لحساب عدد المعاملات المعالجة
        
        async with current_app.db_pool.acquire() as connection:
            # تنفيذ استعلام البيانات
            rows_result = await connection.fetch(query, *query_final_params)
            rows_data = [dict(row) for row in rows_result]
            
            # تنفيذ استعلام العدد الكلي (المفلتر)
            count_result_row = await connection.fetchrow(count_query, *where_params)
            if count_result_row and "total" in count_result_row:
                total_records = count_result_row["total"]
            
            # إحصائية عدد المعاملات المعالجة (يمكن أن تكون مفلترة أو غير مفلترة)
            # لجعلها مفلترة، أضف where_sql و where_params. حاليًا هي غير مفلترة.
            # لجعلها تتبع نفس الفلاتر (باستثناء فلتر processed نفسه إذا أردت إجمالي المعالج بغض النظر عن الفلتر الحالي):
            processed_count_where_clauses = list(where_clauses) # ابدأ بنسخة من الفلاتر الرئيسية
            processed_count_where_params = list(where_params)

            # تأكد من أن فلتر `processed` موجود ويشير إلى `true`
            # إذا كان الفلتر الرئيسي لـ `processed` هو 'all' أو 'false'، فسنحتاج لتجاوزه هنا
            # أو بناء where_clauses جديدة خصيصًا لهذا العد
            
            # أبسط طريقة هي عد المعاملات المعالجة التي تطابق الفلاتر *الأخرى*
            specific_processed_where_clauses = [wc for wc in where_clauses if "it.processed" not in wc]
            specific_processed_where_params = [wp for i, wp in enumerate(where_params) if "it.processed" not in where_clauses[i+1]] # (i+1) لأن where_clauses[0] هو '1=1'

            specific_processed_where_clauses.append(f"it.processed = ${len(specific_processed_where_params) + 1}")
            specific_processed_where_params.append(True)
            
            processed_count_where_sql = " AND ".join(specific_processed_where_clauses) if len(specific_processed_where_clauses) > 1 else specific_processed_where_clauses[0]

            processed_count_query_sql = f"""
                SELECT COUNT(it.txhash) as processed_count
                FROM incoming_transactions it
                WHERE {processed_count_where_sql}
            """
            processed_count_result_row = await connection.fetchrow(processed_count_query_sql, *specific_processed_where_params)
            if processed_count_result_row and "processed_count" in processed_count_result_row:
                processed_transactions_count = processed_count_result_row["processed_count"]


        response = {
            "data": rows_data,
            "total": total_records, # تم التغيير من totalCount إلى total
            "page": page,
            "page_size": page_size, # تم التغيير من pageSize إلى page_size
            "processed_count": processed_transactions_count, # إحصائية جديدة
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
@role_required("admin") 
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
@admin_routes.route("/subscriptions", methods=["POST"])
@role_required("admin")
async def add_subscription():
    try:
        data = await request.get_json()
        # استلام البيانات الأساسية من الـ Modal
        telegram_id = data.get("telegram_id")
        expiry_date = data.get("expiry_date")
        subscription_type_id = data.get("subscription_type_id")
        full_name = data.get("full_name")
        username = data.get("username")

        # التحقق من وجود الحقول الأساسية
        if not all([telegram_id, subscription_type_id, expiry_date]):
            return jsonify({"error": "Missing required fields: telegram_id, subscription_type_id, expiry_date"}), 400

        # تحويل telegram_id إلى رقم (int)
        try:
            telegram_id = int(telegram_id)
        except ValueError:
            return jsonify({"error": "Invalid telegram_id format"}), 400

        from datetime import datetime
        import pytz
        local_tz = pytz.timezone("Asia/Riyadh")
        # تحويل expiry_date إلى datetime timezone-aware باستخدام local_tz
        dt_expiry = datetime.fromisoformat(expiry_date.replace("Z", "")).replace(tzinfo=pytz.UTC).astimezone(local_tz)

        async with current_app.db_pool.acquire() as connection:
            # استنتاج channel_id من subscription_type_id
            channel_row = await connection.fetchrow(
                "SELECT channel_id FROM subscription_types WHERE id = $1",
                subscription_type_id
            )
            if not channel_row:
                return jsonify({"error": "Invalid subscription_type_id"}), 400
            channel_id = channel_row["channel_id"]

            # التعامل مع user_id: البحث عن المستخدم باستخدام telegram_id
            user_row = await connection.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                telegram_id
            )
            if user_row:
                user_id = user_row["id"]
            else:
                # إنشاء سجل مستخدم جديد إذا لم يكن موجوداً
                user_insert = await connection.fetchrow(
                    "INSERT INTO users (telegram_id, username, full_name) VALUES ($1, $2, $3) RETURNING id",
                    telegram_id, username, full_name
                )
                user_id = user_insert["id"]

            # تعيين subscription_plan_id افتراضيًا إذا لم يُرسل
            subscription_plan_id = data.get("subscription_plan_id") or 1

            # ضبط is_active بناءً على expiry_date
            is_active = dt_expiry > datetime.now(local_tz)

            # تعيين source افتراضيًا
            source = data.get("source") or "manual"

            # payment_id يبقى None إذا لم يُرسل
            payment_id = data.get("payment_id")

            query = """
                INSERT INTO subscriptions 
                (user_id, expiry_date, is_active, channel_id, subscription_type_id, telegram_id, subscription_plan_id, payment_id, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *;
            """
            row = await connection.fetchrow(
                query,
                user_id,
                dt_expiry,
                is_active,
                channel_id,
                subscription_type_id,
                telegram_id,
                subscription_plan_id,
                payment_id,
                source
            )
        return jsonify(dict(row)), 201
    except Exception as e:
        logging.error("Error adding subscription: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/subscriptions/export", methods=["GET"])
@role_required("admin")
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
@role_required("owner")
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
@role_required("owner")
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
@role_required("admin")
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
@role_required("admin")
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
@role_required("admin")
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
@role_required("admin")
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
@role_required("admin")
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