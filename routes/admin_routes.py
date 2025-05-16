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

@admin_routes.route("/users", methods=["GET"])
@role_required("admin")
async def get_users():
    try:
        # --- معالجة المعلمات من الطلب ---
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        search_term = request.args.get("search", "").strip()

        # معالجة الترتيب
        ordering = request.args.get("ordering", "-id")  # افتراضيًا الأحدث حسب id
        sort_field_map = {
            "id": "u.id",
            "telegram_id": "u.telegram_id",
            "username": "u.username",
            "full_name": "u.full_name",
        }

        order_by_clause = "ORDER BY u.id DESC"  # افتراضي
        if ordering:
            sort_order = "DESC" if ordering.startswith("-") else "ASC"
            field_key = ordering.lstrip("-")
            if field_key in sort_field_map:
                order_by_clause = f"ORDER BY {sort_field_map[field_key]} {sort_order}"
            else:
                logging.warning(f"Unsupported sort field: {field_key}")

        # --- بناء الاستعلام ---
        params = []
        conditions = []

        # فلتر البحث
        if search_term:
            search_pattern = f"%{search_term}%"
            conditions.append(
                f"""(u.telegram_id::TEXT ILIKE ${len(params) + 1} OR 
                     u.full_name ILIKE ${len(params) + 1} OR 
                     u.username ILIKE ${len(params) + 1})"""
            )
            params.append(search_pattern)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # --- استعلام العدد الإجمالي ---
        count_query = f"""
            SELECT COUNT(u.id) AS total_count
            FROM users u
            WHERE {where_clause}
        """

        # --- استعلام البيانات ---
        offset_val = (page - 1) * page_size
        data_query_params = list(params)  # نسخة من params للـ data query
        data_query_params.extend([page_size, offset_val])

        data_query = f"""
            SELECT u.id, u.telegram_id, u.username, u.full_name, u.wallet_address, u.ton_wallet_address, u.wallet_app,
            (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id) as subscription_count,
            (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id AND s.is_active = true) as active_subscription_count
            FROM users u
            WHERE {where_clause}
            {order_by_clause}
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """

        async with current_app.db_pool.acquire() as conn:
            # استعلام العدد
            count_row = await conn.fetchrow(count_query, *params)
            total_items = count_row['total_count'] if count_row else 0

            # استعلام البيانات
            items_data = []
            if total_items > 0 and offset_val < total_items:
                rows = await conn.fetch(data_query, *data_query_params)
                items_data = [dict(row) for row in rows]

        return jsonify({
            "data": items_data,
            "total_count": total_items,
            "page": page,
            "page_size": page_size
        })

    except ValueError as ve:
        logging.error(f"Value error in /users: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters"}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /users: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed"}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /users: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


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
async def get_subscriptions_endpoint():  # تم تغيير الاسم لتمييزه عن دالة الواجهة الأمامية
    try:
        # --- Parameters from Request ---
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        search_term = request.args.get("search", "").strip()

        # الفرز (ordering من الواجهة الأمامية مثل "-id" أو "expiry_date")
        ordering = request.args.get("ordering", "-id")  # افتراضيًا الأحدث حسب id
        sort_field_map = {
            "id": "s.id",
            "telegram_id": "s.telegram_id",
            "username": "u.username",  # يتطلب JOIN
            "full_name": "u.full_name",  # يتطلب JOIN
            "expiry_date": "s.expiry_date",
            "is_active": "s.is_active",
            "subscription_type_name": "st.name",  # يتطلب JOIN
            "source": "s.source",
            # أضف أي حقول فرز أخرى تدعمها
        }

        order_by_clause = "ORDER BY s.id DESC"  # افتراضي
        if ordering:
            sort_order = "DESC" if ordering.startswith("-") else "ASC"
            field_key = ordering.lstrip("-")
            if field_key in sort_field_map:
                order_by_clause = f"ORDER BY {sort_field_map[field_key]} {sort_order}"
            else:
                logging.warning(f"Unsupported sort field: {field_key}")
                # يمكنك إرجاع خطأ 400 هنا أو استخدام الفرز الافتراضي

        # الفلاتر
        status_filter = request.args.get(
            "status")  # "active", "inactive", أو "true", "false" من الواجهة الأمامية القديمة
        type_filter = request.args.get("type")  # subscription_type_id
        source_filter = request.args.get("source")
        start_date_filter = request.args.get("startDate")  # YYYY-MM-DD
        end_date_filter = request.args.get("endDate")  # YYYY-MM-DD

        # --- Build Query ---
        params = []
        conditions = []  # قائمة بالشروط (WHERE clauses)

        # Filter: Search Term
        if search_term:
            search_pattern = f"%{search_term}%"
            # البحث في telegram_id كـ TEXT، وفي full_name و username من جدول users
            # يتطلب JOIN مع users إذا لم يكن موجودًا بالفعل
            conditions.append(
                f"""(s.telegram_id::TEXT ILIKE ${len(params) + 1} OR 
                     u.full_name ILIKE ${len(params) + 1} OR 
                     u.username ILIKE ${len(params) + 1})"""
            )
            params.append(search_pattern)

        # Filter: Status (is_active)
        if status_filter is not None:
            # الواجهة الأمامية ترسل "true" أو "false" كسلسلة نصية للفلتر
            # أو قد ترسل "active" / "inactive"
            is_active_bool = None
            if isinstance(status_filter, str):
                if status_filter.lower() == "true" or status_filter.lower() == "active":
                    is_active_bool = True
                elif status_filter.lower() == "false" or status_filter.lower() == "inactive":
                    is_active_bool = False

            if is_active_bool is not None:
                conditions.append(f"s.is_active = ${len(params) + 1}")
                params.append(is_active_bool)
            else:
                logging.warning(f"Invalid status filter value: {status_filter}")

        # Filter: Subscription Type ID
        if type_filter:
            try:
                conditions.append(f"s.subscription_type_id = ${len(params) + 1}")
                params.append(int(type_filter))
            except ValueError:
                logging.warning(f"Invalid subscription_type_id filter: {type_filter}")
                # يمكنك إرجاع خطأ 400 هنا

        # Filter: Source
        if source_filter:
            conditions.append(f"s.source ILIKE ${len(params) + 1}")  # ILIKE لتكون غير حساسة لحالة الأحرف
            params.append(f"%{source_filter}%")  # بحث جزئي إذا أردت، أو = للبحث المطابق

        # Filter: Date Range (expiry_date)
        if start_date_filter:
            try:
                # تأكد من أن التاريخ بالصيغة الصحيحة أو قم بتحويله
                # dayjs في الواجهة الأمامية يرسل YYYY-MM-DD
                conditions.append(f"s.expiry_date >= ${len(params) + 1}::date")
                params.append(start_date_filter)
            except ValueError:
                logging.warning(f"Invalid startDate filter: {start_date_filter}")

        if end_date_filter:
            try:
                # إضافة يوم واحد لجعل النطاق شاملاً لليوم المحدد
                # أو تعديل المقارنة إلى < اليوم التالي
                # أو استخدام ::date إذا كان expiry_date يخزن الوقت أيضًا وتريد مقارنة الأيام فقط
                conditions.append(
                    f"s.expiry_date <= (${len(params) + 1}::date + INTERVAL '1 day' - INTERVAL '1 second')")
                # أو ببساطة: conditions.append(f"s.expiry_date <= ${len(params) + 1}::date") إذا كان الوقت في expiry_date دائمًا 00:00:00
                # أو إذا كان الواجهة الأمامية ترسل نهاية اليوم
                params.append(end_date_filter)
            except ValueError:
                logging.warning(f"Invalid endDate filter: {end_date_filter}")

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # --- Base SELECT and JOINs ---
        # يتم تحديد الأعمدة المطلوبة هنا
        select_columns = """
            s.id, s.user_id, s.expiry_date, s.is_active, s.channel_id, 
            s.subscription_type_id, s.telegram_id, s.start_date, 
            s.updated_at, s.payment_id, s.subscription_plan_id, s.source,
            u.full_name, u.username, 
            st.name AS subscription_type_name,
            sp.name AS subscription_plan_name 
        """
        # إذا لم تكن دائمًا بحاجة لـ sp.name، يمكنك إزالة الـ JOIN

        from_clause = """
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id  -- أو s.user_id = u.id إذا كان هذا هو الربط الصحيح
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
        """

        # --- Count Query ---
        # استخدام نفس الـ JOINs والـ WHERE clause للاستعلام عن العدد لضمان الدقة
        count_query = f"""
            SELECT COUNT(s.id) AS total_count
            {from_clause}
            WHERE {where_clause}
        """

        # --- Data Query ---
        offset_val = (page - 1) * page_size
        data_query_params = list(params)  # نسخة من params للـ data query
        data_query_params.extend([page_size, offset_val])

        data_query = f"""
            SELECT {select_columns}
            {from_clause}
            WHERE {where_clause}
            {order_by_clause}
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2} 
        """
        # الـ placeholders لـ LIMIT و OFFSET هي بعد params الأصلية

        async with current_app.db_pool.acquire() as conn:
            # Execute count query (using original params, without limit/offset)
            count_row = await conn.fetchrow(count_query, *params)
            total_items_for_filter = count_row['total_count'] if count_row else 0

            # Execute data query only if there's data to fetch
            items_data = []
            if total_items_for_filter > 0 and offset_val < total_items_for_filter:
                rows = await conn.fetch(data_query, *data_query_params)
                items_data = [dict(row) for row in rows]

        return jsonify({
            "data": items_data,
            "total_count": total_items_for_filter,
            "page": page,
            "page_size": page_size
        })

    except ValueError as ve:  # مثل تحويل page أو page_size إلى int
        logging.error(f"Value error in /subscriptions: {str(ve)}", exc_info=True)
        return jsonify({"error": "Invalid request parameters"}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error in /subscriptions: {str(pe)}", exc_info=True)
        return jsonify({"error": "Database operation failed"}), 500
    except Exception as e:
        logging.error(f"Unexpected error in /subscriptions: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


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
async def get_all_pending_subscriptions():  # تم تغيير اسم الدالة ليعكس وظيفتها بشكل أفضل
    try:
        status_filter = request.args.get("status", "all").lower()
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        params = []
        conditions = []  # نبدأ بقائمة فارغة للشروط

        if status_filter != "all":
            conditions.append(f"ps.status = ${len(params) + 1}")
            params.append(status_filter)

        if search_term:
            # البحث في ps.telegram_id مباشرة، وفي u.full_name و u.username من خلال JOIN
            # استخدام نفس الـ placeholder والقيمة للـ ILIKE المتعددة صحيح
            search_param_index = len(params) + 1
            conditions.append(f"""
                (u.full_name ILIKE ${search_param_index} OR 
                 u.username ILIKE ${search_param_index} OR 
                 ps.telegram_id::TEXT ILIKE ${search_param_index})
            """)
            params.append(f"%{search_term}%")

        # إذا لم تكن هناك شروط، استخدم "1=1" لتجنب خطأ SQL
        where_clause = " AND ".join(conditions) if conditions else "1=1"

        data_query = f"""
            SELECT
                ps.id, 
                ps.user_db_id,
                ps.telegram_id, 
                ps.channel_id, 
                ps.subscription_type_id,
                ps.found_at,
                ps.status,
                ps.admin_reviewed_at,
                u.full_name,
                u.username,
                st.name AS subscription_type_name
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.user_db_id = u.id  -- الربط بـ user_db_id لجلب full_name و username
            LEFT JOIN subscription_types st ON ps.subscription_type_id = st.id
            WHERE {where_clause}
            ORDER BY ps.found_at DESC 
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        data_params = params + [page_size, offset]

        count_query = f"""
            SELECT COUNT(ps.id)
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.user_db_id = u.id -- نفس الـ JOIN مطلوب لتطبيق فلتر البحث بشكل صحيح
            WHERE {where_clause}
        """

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(data_query, *data_params)
            total_count_for_filter = await connection.fetchval(count_query, *params)

        return jsonify({
            "data": [dict(row) for row in rows],
            "total_count": total_count_for_filter or 0,
            "page": page,
            "page_size": page_size
        })

    except Exception as e:
        logging.error("Error fetching pending subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


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
async def get_legacy_subscriptions():
    try:
        processed = request.args.get("processed")
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "")
        sort_field = request.args.get("sort_field", "expiry_date")
        sort_order = request.args.get("sort_order", "desc")

        stats_only = request.args.get("stats_only", "false").lower() == "true"

        base_query = """
            FROM legacy_subscriptions ls
            LEFT JOIN subscription_types st ON ls.subscription_type_id = st.id
            WHERE 1=1
        """

        # المعلمات الخاصة بشروط التصفية فقط
        filter_params = []
        filter_conditions = ""

        if processed is not None:
            filter_conditions += f" AND ls.processed = ${len(filter_params) + 1}"
            filter_params.append(processed.lower() == "true")

        if search_term:
            # تأكد من أن لديك عمودًا مناسبًا للبحث عن الاسم الكامل، أو عدل الاستعلام
            filter_conditions += (
                f" AND ls.username ILIKE ${len(filter_params) + 1}"
            )
            filter_params.append(f"%{search_term}%")

        if stats_only:
            stats_query = f"""
                SELECT 
                    COUNT(*) FILTER (WHERE ls.processed = true) AS processed_true,
                    COUNT(*) FILTER (WHERE ls.processed = false) AS processed_false,
                    COUNT(*) AS total
                {base_query}
                {filter_conditions}
            """
            async with current_app.db_pool.acquire() as connection:
                # استخدم filter_params هنا لأنها لا تتضمن page_size أو offset
                stats = await connection.fetchrow(stats_query, *filter_params)
            return jsonify({
                "processed": stats["processed_true"] if stats else 0,
                "not_processed": stats["not_processed"] if stats else 0,
                "total": stats["total"] if stats else 0
            })

        count_query = f"""
            SELECT COUNT(*) as total_count
            {base_query}
            {filter_conditions}
        """

        # الآن نبني المعلمات الكاملة لـ data_query
        data_query_params = list(filter_params)  # ابدأ بنسخة من filter_params

        data_query = f"""
            SELECT
                ls.*,
                st.name AS subscription_type_name
                -- لا حاجة لإعادة حساب الإجماليات في كل صف هنا إذا كنا سنجلبها مرة واحدة
                -- (SELECT COUNT(*) FROM legacy_subscriptions WHERE processed = true) AS total_processed,
                -- (SELECT COUNT(*) FROM legacy_subscriptions WHERE processed = false) AS total_not_processed,
                -- (SELECT COUNT(*) FROM legacy_subscriptions) AS total_all_subscriptions -- تم تغيير الاسم ليكون أوضح
            {base_query}
            {filter_conditions}
            ORDER BY ls.{sort_field} {sort_order}
            LIMIT ${len(data_query_params) + 1} OFFSET ${len(data_query_params) + 2}
        """

        data_query_params.append(page_size)
        data_query_params.append(offset)

        total_count_val = 0  # قيمة افتراضية
        result = []

        async with current_app.db_pool.acquire() as connection:
            # تنفيذ استعلام العد باستخدام filter_params (التي لا تحتوي على page_size/offset)
            count_row = await connection.fetchrow(count_query, *filter_params)
            total_count_val = count_row["total_count"] if count_row else 0

            # تنفيذ استعلام البيانات باستخدام data_query_params (التي تحتوي على كل شيء)
            if total_count_val > 0:  # لا تجلب البيانات إذا لم يكن هناك شيء
                rows = await connection.fetch(data_query, *data_query_params)
                result = [dict(row) for row in rows]

        # إضافة total_count مرة واحدة إلى الاستجابة بدلاً من كل صف
        # الواجهة الأمامية في React كانت تتوقع totalCount ك prop منفصل، وهو أفضل.
        # إذا كنت تريد إعادته مع كل صف، يمكنك إضافته هنا:
        # for row_dict in result:
        #    row_dict["total_count"] = total_count_val # هذا هو العدد الإجمالي *المطابق للفلتر الحالي*

        # الواجهة الأمامية (الكود الذي قدمته لي) تتوقع أن يكون total_count في كل عنصر من legacySubscriptions
        # إذا كان هذا هو المطلوب:
        if result:
            for row_data in result:
                row_data["total_count"] = total_count_val

        # ولكن عادةً ما يكون من الأفضل إرسال إجمالي عدد العناصر المطابقة مرة واحدة
        # return jsonify({"subscriptions": result, "total_items_for_filter": total_count_val})
        # بما أن الكود الأمامي يتوقع total_count في كل عنصر، سألتزم بذلك مؤقتًا.
        return jsonify(result)

    except Exception as e:
        logging.error("Error fetching legacy subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


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
        status = request.args.get("status")
        user_id = request.args.get("user_id")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "")

        # الاستعلام الأساسي مع الحقول الجديدة
        query = """
            SELECT 
                p.*, 
                sp.name AS plan_name,
                st.name AS subscription_name
            FROM payments p
            JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
            JOIN subscription_types st ON sp.subscription_type_id = st.id
            WHERE 1=1
        """
        params = []

        # تصفية الدفعات حسب الحالة المطلوبة (ناجحة أو فاشلة)
        # في حالة البحث عن الدفعات المعلقة فقط، يمكن استخدام شرط منفصل (مثلاً: status = 'pending')
        if status:
            query += f" AND p.status = ${len(params) + 1}"
            params.append(status)
        else:
            # جلب الدفعات الناجحة والفاشلة بشكل افتراضي
            query += f" AND p.status IN ('completed', 'failed' )"

        if user_id:
            query += f" AND p.user_id = ${len(params) + 1}"
            params.append(user_id)
        if start_date:
            query += f" AND p.created_at >= ${len(params) + 1}"
            params.append(start_date)
        if end_date:
            query += f" AND p.created_at <= ${len(params) + 1}"
            params.append(end_date)

        # إضافة شرط البحث باستخدام الحقول المطلوبة
        if search_term:
            query += f" AND (p.tx_hash ILIKE ${len(params) + 1} OR p.payment_token ILIKE ${len(params) + 1} OR p.username ILIKE ${len(params) + 1} OR p.telegram_id::TEXT ILIKE ${len(params) + 1})"
            params.append(f"%{search_term}%")

        # ترتيب النتائج من الأحدث إلى الأقدم
        query += f" ORDER BY p.created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params.append(page_size)
        params.append(offset)

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *params)

        # التقارير المالية (مثال: إجمالي الإيرادات)
        total_revenue = 0
        if request.args.get("report") == "total_revenue":
            rev_query = "SELECT SUM(amount) as total FROM payments WHERE 1=1"
            rev_params = []
            if status:
                rev_query += f" AND status = ${len(rev_params) + 1}"
                rev_params.append(status)
            if user_id:
                rev_query += f" AND user_id = ${len(rev_params) + 1}"
                rev_params.append(user_id)
            if start_date:
                rev_query += f" AND created_at >= ${len(rev_params) + 1}"
                rev_params.append(start_date)
            if end_date:
                rev_query += f" AND created_at <= ${len(rev_params) + 1}"
                rev_params.append(end_date)
            async with current_app.db_pool.acquire() as connection:
                rev_row = await connection.fetchrow(rev_query, *rev_params)
            total_revenue = rev_row["total"] if rev_row["total"] is not None else 0

        response = {
            "data": [dict(row) for row in rows],
            "total_revenue": total_revenue
        }
        return jsonify(response)
    except Exception as e:
        logging.error("Error fetching payments: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@admin_routes.route("/incoming-transactions", methods=["GET"])
@role_required("admin")
async def get_incoming_transactions():
    try:
        search_term = request.args.get("search", "").strip()  # .strip() لإزالة المسافات
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size

        base_query = """
            FROM incoming_transactions it
            WHERE 1=1
        """
        count_query_sql = "SELECT COUNT(*) " + base_query
        data_query_sql = """
            SELECT 
                it.txhash,
                it.sender_address,
                it.amount,
                it.payment_token,
                it.processed,
                it.received_at,
                it.memo
        """ + base_query

        params = []
        count_params = []

        where_clauses = []

        if search_term:
            # ل postgres، المعاملات المرقمة تبدأ من $1
            # سنستخدم نفس المعامل للبحث في عدة حقول
            search_param_placeholder = f"${len(params) + 1}"
            where_clauses.append(
                f"(it.txhash ILIKE {search_param_placeholder} OR it.sender_address ILIKE {search_param_placeholder} OR it.memo ILIKE {search_param_placeholder})")
            params.append(f"%{search_term}%")
            # نفس المعاملات لـ count_params
            count_params.append(f"%{search_term}%")

        if where_clauses:
            data_query_sql += " AND " + " AND ".join(where_clauses)
            count_query_sql += " AND " + " AND ".join(where_clauses)  # استخدام نفس where_clauses لـ count

        # إضافة الترتيب والتجزئة فقط لاستعلام البيانات
        data_query_sql += f" ORDER BY it.received_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params.append(page_size)
        params.append(offset)

        async with current_app.db_pool.acquire() as connection:
            total_count_record = await connection.fetchrow(count_query_sql, *count_params)
            total_count = total_count_record[0] if total_count_record else 0

            rows = await connection.fetch(data_query_sql, *params)

        return jsonify({
            "data": [dict(row) for row in rows],
            "totalCount": total_count,
            "page": page,
            "pageSize": page_size
        })

    except Exception as e:
        logging.error("Error fetching incoming transactions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# =====================================
# 3. API لتعديل اشتراك مستخدم
# =====================================
@admin_routes.route("/subscriptions/<int:subscription_id>", methods=["PUT"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
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