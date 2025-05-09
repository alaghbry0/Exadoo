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


@admin_routes.route("/users", methods=["GET"])
@role_required("owner")
async def get_users():
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


#######################################
# نقاط API لإدارة أنواع الاشتراكات (subscription_types)
#######################################

# إضافة نوع اشتراك جديد
@admin_routes.route("/subscription-types", methods=["POST"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
async def create_subscription_type():
    try:
        data = await request.get_json()
        name = data.get("name")
        channel_id = data.get("channel_id")
        description = data.get("description", "")
        image_url = data.get("image_url", "")
        features = data.get("features", [])  # يجب أن يكون قائمة (List)
        usp = data.get("usp", "")
        is_active = data.get("is_active", True)

        if not name or channel_id is None:
            return jsonify({"error": "Missing required fields: name and channel_id"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                INSERT INTO subscription_types
                (name, channel_id, description, image_url, features, usp, is_active)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                RETURNING id, name, channel_id, description, image_url, features, usp, is_active, created_at;
            """
            result = await connection.fetchrow(
                query,
                name,
                channel_id,
                description,
                image_url,
                json.dumps(features),
                usp,
                is_active
            )
        return jsonify(dict(result)), 201
    except Exception as e:
        logging.error("Error creating subscription type: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# تعديل بيانات نوع اشتراك موجود
@admin_routes.route("/subscription-types/<int:type_id>", methods=["PUT"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
async def update_subscription_type(type_id: int):
    try:
        data = await request.get_json()
        name = data.get("name")
        channel_id = data.get("channel_id")
        description = data.get("description")
        image_url = data.get("image_url")
        features = data.get("features")
        usp = data.get("usp")
        is_active = data.get("is_active")

        async with current_app.db_pool.acquire() as connection:
            query = """
                UPDATE subscription_types
                SET name = COALESCE($1, name),
                    channel_id = COALESCE($2, channel_id),
                    description = COALESCE($3, description),
                    image_url = COALESCE($4, image_url),
                    features = COALESCE($5::jsonb, features),
                    usp = COALESCE($6, usp),
                    is_active = COALESCE($7, is_active)
                WHERE id = $8
                RETURNING id, name, channel_id, description, image_url, features, usp, is_active, created_at;
            """
            features_json = json.dumps(features) if features is not None else None
            result = await connection.fetchrow(query, name, channel_id, description, image_url, features_json, usp,
                                               is_active, type_id)
        if result:
            return jsonify(dict(result)), 200
        else:
            return jsonify({"error": "Subscription type not found"}), 404
    except Exception as e:
        logging.error("Error updating subscription type: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# حذف نوع اشتراك
@admin_routes.route("/subscription-types/<int:type_id>", methods=["DELETE"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
async def delete_subscription_type(type_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            query = "DELETE FROM subscription_types WHERE id = $1"
            await connection.execute(query, type_id)
        return jsonify({"message": "Subscription type deleted successfully"}), 200
    except Exception as e:
        logging.error("Error deleting subscription type: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# جلب قائمة أنواع الاشتراكات
@admin_routes.route("/subscription-types", methods=["GET"])
@role_required("owner")  # ✅ استخدام @role_required("admin")
async def get_subscription_types():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, name, channel_id, description, image_url, features, usp, is_active, created_at
                FROM subscription_types
                ORDER BY created_at DESC
            """
            results = await connection.fetch(query)

        types = []
        for row in results:
            row_dict = dict(row)
            row_dict["features"] = json.loads(row_dict["features"]) if row_dict["features"] else []
            types.append(row_dict)

        return jsonify(types), 200, {"Content-Type": "application/json; charset=utf-8"}

    except Exception as e:
        logging.error("Error fetching subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# جلب تفاصيل نوع اشتراك معين
@admin_routes.route("/subscription-types/<int:type_id>", methods=["GET"])
@role_required("admin")  # ✅ استخدام @role_required("admin")
async def get_subscription_type(type_id: int):
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, name, channel_id, description, image_url, features, usp, is_active, created_at
                FROM subscription_types
                WHERE id = $1
            """
            result = await connection.fetchrow(query, type_id)
        if result:
            return jsonify(dict(result)), 200
        else:
            return jsonify({"error": "Subscription type not found"}), 404
    except Exception as e:
        logging.error("Error fetching subscription type: %s", e, exc_info=True)
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
async def get_subscriptions():
    try:
        # الحصول على المتغيرات من استعلام URL
        user_id = request.args.get("user_id")
        channel_id = request.args.get("channel_id")
        status = request.args.get("status")  # active أو inactive
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()
        source = request.args.get("source")
        subscription_type_id_filter = request.args.get("type")

        # استعلام SQL الأساسي
        base_query = """
            SELECT
                s.*,
                u.full_name,
                u.username,
                sp.name AS subscription_plan_name,
                st.name AS subscription_type_name
            FROM subscriptions s
            LEFT JOIN users u ON s.telegram_id = u.telegram_id
            LEFT JOIN subscription_plans sp ON s.subscription_plan_id = sp.id
            LEFT JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE 1=1
        """
        params = []
        conditions = []

        # بناء الشروط الديناميكية
        if user_id:
            conditions.append(f"s.telegram_id = ${len(params)+1}")
            params.append(user_id)
        if channel_id:
            conditions.append(f"s.channel_id = ${len(params)+1}")
            params.append(channel_id)
        if status:
            status_bool = "true" if status.lower() == "active" else "false"
            conditions.append(f"s.is_active = {status_bool}")
        if start_date:
            conditions.append(f"s.expiry_date >= ${len(params)+1}::timestamptz")
            params.append(start_date)
        if end_date:
            conditions.append(f"s.expiry_date <= ${len(params)+1}::timestamptz")
            params.append(end_date)
        if search_term:
            conditions.append(
                f"(u.full_name ILIKE ${len(params)+1} OR "
                f"u.username ILIKE ${len(params)+1} OR "
                f"s.telegram_id::TEXT ILIKE ${len(params)+1})"
            )
            params.append(f"%{search_term}%")
        if source:
            conditions.append(f"s.source = ${len(params)+1}")
            params.append(source)
        if subscription_type_id_filter:
            try:
                conditions.append(f"s.subscription_type_id = ${len(params)+1}")
                params.append(int(subscription_type_id_filter))
            except ValueError:
                logging.warning(f"Invalid subscription_type_id: {subscription_type_id_filter}")

        # بناء الاستعلام النهائي
        query = base_query
        if conditions:
            query += " AND " + " AND ".join(conditions)

        # استعلام العد المحسن
        count_query = f"""
            SELECT COUNT(*) AS total_count 
            FROM ({query.replace('s.*, u.full_name, u.username', 's.id')}) AS subquery
        """.split("ORDER BY")[0]

        # إضافة التجزئة والترتيب
        paginated_query = query + f" ORDER BY s.expiry_date DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        params.extend([page_size, offset])

        async with current_app.db_pool.acquire() as conn:
            # تنفيذ استعلام العد
            try:
                count_params = params[:-2]
                count_row = await conn.fetchrow(count_query, *count_params)
                total = count_row['total_count'] if count_row else 0
            except asyncpg.UndefinedColumnError:
                total = 0
                logging.warning("Count query column mismatch, falling back to 0")

            # تنفيذ الاستعلام الرئيسي
            rows = await conn.fetch(paginated_query, *params)

        # بناء النتيجة
        results = [dict(row) for row in rows]
        for item in results:
            item['total_count'] = total

        return jsonify(results)

    except ValueError as ve:
        logging.error(f"Validation error: {str(ve)}")
        return jsonify({"error": "Invalid request parameters"}), 400
    except asyncpg.PostgresError as pe:
        logging.error(f"Database error: {str(pe)}")
        return jsonify({"error": "Database operation failed"}), 500
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}", exc_info=True)
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


# تعديل واجهة API للحصول على pending_subscriptions لدعم الفلترة بشكل أفضل
@admin_routes.route("/pending_subscriptions", methods=["GET"])
@role_required("admin")
async def get_pending_subscriptions():
    try:
        # الفلتر status: يمكن أن يكون "all", "pending", "complete"
        status_filter = request.args.get("status", "all") # الافتراضي هو "all" إذا لم يحدد
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size
        search_term = request.args.get("search", "").strip()

        params = []
        conditions = ["1=1"] # Starting with a base condition

        # بناء جزء WHERE بشكل ديناميكي
        if status_filter and status_filter.lower() != "all":
            conditions.append(f"ps.status = ${len(params) + 1}")
            params.append(status_filter)

        if search_term:
            # ILIKE للبحث غير الحساس لحالة الأحرف
            # تأكد من أن أعمدة البحث مفهرسة جيدًا في قاعدة البيانات للأداء
            search_condition = f"""
                (u.full_name ILIKE ${len(params) + 1} OR 
                 u.username ILIKE ${len(params) + 1} OR 
                 ps.telegram_id::TEXT ILIKE ${len(params) + 1}) 
            """ # استخدام نفس placeholder لأن القيمة نفسها
            conditions.append(search_condition)
            params.append(f"%{search_term}%")
            # إذا كنت تستخدم placeholders مختلفة لكل ILIKE (وهو أكثر أمانًا ودقة)
            # params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])
            # وفي هذه الحالة، عدّادات placeholder ستكون ${len(params)+1}, ${len(params)+2}, ${len(params)+3}


        where_clause = " AND ".join(conditions)

        # استعلام جلب البيانات
        data_query = f"""
            SELECT
                ps.*,
                u.full_name,
                u.username,
                st.name AS subscription_type_name
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.telegram_id = u.telegram_id
            LEFT JOIN subscription_types st ON ps.subscription_type_id = st.id
            WHERE {where_clause}
            ORDER BY ps.found_at DESC 
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        data_params = params + [page_size, offset]

        # استعلام جلب العدد الإجمالي للبيانات المفلترة (مهم للـ pagination)
        count_query = f"""
            SELECT COUNT(ps.id)
            FROM pending_subscriptions ps
            LEFT JOIN users u ON ps.telegram_id = u.telegram_id 
            WHERE {where_clause}
        """
        # params للـ count هي نفسها بدون page_size و offset

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(data_query, *data_params)
            total_count_for_filter = await connection.fetchval(count_query, *params) # استخدام params الأصلية للفلتر

        return jsonify({
            "data": [dict(row) for row in rows],
            "total_count": total_count_for_filter or 0, # العدد الإجمالي بناءً على الفلتر
            "page": page,
            "page_size": page_size
        })

    except Exception as e:
        logging.error("Error fetching pending subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# واجهة API إضافية للتعامل مع إجراءات pending_subscriptions (قبول/رفض)
@admin_routes.route("/pending_subscriptions/<int:id>/action", methods=["POST"])
@role_required("admin")
async def handle_pending_subscription(id):
    try:
        if not request.is_json:
            return jsonify({"error": "Request body must be JSON"}), 415

        data = await request.get_json()
        if data is None:
            return jsonify({"error": "Invalid JSON payload"}), 400

        action = data.get('action')

        # نتوقع الآن فقط 'mark_as_complete'
        if action != 'mark_as_complete':
            return jsonify({"error": f"Invalid action: '{action}'. Expected 'mark_as_complete'."}), 400

        async with current_app.db_pool.acquire() as connection:
            pending_sub = await connection.fetchrow(
                "SELECT * FROM pending_subscriptions WHERE id = $1", id
            )

            if not pending_sub:
                return jsonify({"error": "Pending subscription not found"}), 404

            if pending_sub['status'] == 'complete':
                return jsonify({"success": True, "message": "Subscription already marked as complete."}), 200 # OK, no change

            if pending_sub['status'] != 'pending':
                return jsonify({"error": f"Subscription is not in 'pending' state (current: {pending_sub['status']}). Cannot mark as complete."}), 400

            # تم إزالة الجزء الخاص بإنشاء اشتراك جديد في جدول subscriptions

            # تحديث حالة الاشتراك المعلق إلى 'complete'
            await connection.execute("""
                UPDATE pending_subscriptions
                SET status = 'complete', admin_reviewed_at = NOW()
                WHERE id = $1
            """, id) # الحالة الجديدة 'complete'

        # تم تعديل رسالة النجاح لتعكس أننا فقط نحدث الحالة
        return jsonify({"success": True, "message": "Pending subscription marked as complete."})

    except Exception as e:
        logging.error("Error marking pending subscription as complete: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    




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
            filter_conditions += f" AND (ls.username ILIKE ${len(filter_params) + 1} OR ls.full_name ILIKE ${len(filter_params) + 2})" # لاحظ أن هذا يضيف عنصرين نائبين
            search_pattern = f"%{search_term}%"
            filter_params.append(search_pattern) # لـ username
            filter_params.append(search_pattern) # لـ full_name
        
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
        data_query_params = list(filter_params) # ابدأ بنسخة من filter_params
        
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
        
        total_count_val = 0 # قيمة افتراضية
        result = []

        async with current_app.db_pool.acquire() as connection:
            # تنفيذ استعلام العد باستخدام filter_params (التي لا تحتوي على page_size/offset)
            count_row = await connection.fetchrow(count_query, *filter_params)
            total_count_val = count_row["total_count"] if count_row else 0
            
            # تنفيذ استعلام البيانات باستخدام data_query_params (التي تحتوي على كل شيء)
            if total_count_val > 0: # لا تجلب البيانات إذا لم يكن هناك شيء
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
        processed_filter = request.args.get("processed") # "true", "false", or None

        query_base = "FROM legacy_subscriptions"
        query_conditions = ""
        params = []

        if processed_filter is not None:
            query_conditions += " WHERE processed = $1"
            params.append(processed_filter.lower() == "true")
        
        # استعلام واحد للإحصائيات
        query = f"""
            SELECT 
                COUNT(*) FILTER (WHERE processed = true { 'AND processed = $1' if processed_filter == 'true' else '' } { 'AND processed = $1' if processed_filter == 'false' else '' } ) AS processed_true,
                COUNT(*) FILTER (WHERE processed = false { 'AND processed = $1' if processed_filter == 'true' else '' } { 'AND processed = $1' if processed_filter == 'false' else '' } ) AS processed_false,
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
            params = [] # لا معلمات للاستعلام الإجمالي
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
            "true": stats["processed_true"] if stats else 0, # مطابقة للمفتاح في React
            "false": stats["processed_false"] if stats else 0, # مطابقة للمفتاح في React
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
        search_term = request.args.get("search", "")
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        offset = (page - 1) * page_size

        query = """
            SELECT 
                it.txhash,
                it.sender_address,
                it.amount,
                it.payment_token,
                it.processed,
                it.received_at,
                it.memo
            FROM incoming_transactions it
            WHERE 1=1
        """
        params = []

        # إضافة شرط البحث إذا تم تمرير search_term
        if search_term:
            query += f" AND (it.txhash ILIKE ${len(params) + 1} OR it.sender_address ILIKE ${len(params) + 1} OR it.memo ILIKE ${len(params) + 1})"
            params.append(f"%{search_term}%")

        # ترتيب النتائج وتطبيق التجزئة
        query += f" ORDER BY it.received_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params.append(page_size)
        params.append(offset)

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *params)
        return jsonify([dict(row) for row in rows])
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
        if not all([telegram_id, expiry_date, subscription_type_id, full_name, username]):
            return jsonify({"error": "Missing required fields"}), 400

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