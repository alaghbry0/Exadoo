import os
import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import json
from datetime import datetime



# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)
# إنشاء Blueprint مع بادئة URL
admin_routes = Blueprint("admin_routes", __name__, url_prefix="/api/admin")



# دالة تحقق بسيطة للتأكد من صلاحية المستخدم (admin)
def is_admin():
    auth_header = request.headers.get("Authorization")
    admin_token = os.getenv("ADMIN_TOKEN")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        return token == admin_token
    return False

#######################################
# نقاط API لإدارة أنواع الاشتراكات (subscription_types)
#######################################

# إضافة نوع اشتراك جديد
@admin_routes.route("/subscription-types", methods=["POST"])
async def create_subscription_type():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
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
async def update_subscription_type(type_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
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
            result = await connection.fetchrow(query, name, channel_id, description, image_url, features_json, usp, is_active, type_id)
        if result:
            return jsonify(dict(result)), 200
        else:
            return jsonify({"error": "Subscription type not found"}), 404
    except Exception as e:
        logging.error("Error updating subscription type: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# حذف نوع اشتراك
@admin_routes.route("/subscription-types/<int:type_id>", methods=["DELETE"])
async def delete_subscription_type(type_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
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
async def get_subscription_types():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, name, channel_id, description, image_url, features, usp, is_active, created_at
                FROM subscription_types
                ORDER BY created_at DESC
            """
            results = await connection.fetch(query)

        # تحويل النتائج إلى JSON مع التأكد من الترميز
        types = []
        for row in results:
            row_dict = dict(row)

            # تأكد من أن `features` مخزن كـ JSON وليس كنص
            row_dict["features"] = json.loads(row_dict["features"]) if row_dict["features"] else []

            types.append(row_dict)

        # إرجاع البيانات مع تحديد `Content-Type`
        return jsonify(types), 200, {"Content-Type": "application/json; charset=utf-8"}

    except Exception as e:
        logging.error("Error fetching subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
# جلب تفاصيل نوع اشتراك معين
@admin_routes.route("/subscription-types/<int:type_id>", methods=["GET"])
async def get_subscription_type(type_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
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

# إضافة خطة اشتراك جديدة لنوع معين
@admin_routes.route("/subscription-plans", methods=["POST"])
async def create_subscription_plan():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        data = await request.get_json()
        subscription_type_id = data.get("subscription_type_id")
        name = data.get("name")
        price = data.get("price")
        duration_days = data.get("duration_days")
        telegram_stars_price = data.get("telegram_stars_price", 0)
        is_active = data.get("is_active", True)

        if not subscription_type_id or not name or price is None or duration_days is None:
            return jsonify({"error": "Missing required fields"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                INSERT INTO subscription_plans
                (subscription_type_id, name, price, telegram_stars_price, duration_days, is_active)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at;
            """
            result = await connection.fetchrow(
                query,
                subscription_type_id,
                name,
                price,
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
async def update_subscription_plan(plan_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        data = await request.get_json()
        subscription_type_id = data.get("subscription_type_id")
        name = data.get("name")
        price = data.get("price")
        duration_days = data.get("duration_days")
        telegram_stars_price = data.get("telegram_stars_price")
        is_active = data.get("is_active")

        async with current_app.db_pool.acquire() as connection:
            query = """
                UPDATE subscription_plans
                SET subscription_type_id = COALESCE($1, subscription_type_id),
                    name = COALESCE($2, name),
                    price = COALESCE($3, price),
                    telegram_stars_price = COALESCE($4, telegram_stars_price),
                    duration_days = COALESCE($5, duration_days),
                    is_active = COALESCE($6, is_active)
                WHERE id = $7
                RETURNING id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at;
            """
            result = await connection.fetchrow(
                query,
                subscription_type_id,
                name,
                price,
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

# حذف خطة اشتراك
@admin_routes.route("/subscription-plans/<int:plan_id>", methods=["DELETE"])
async def delete_subscription_plan(plan_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        async with current_app.db_pool.acquire() as connection:
            query = "DELETE FROM subscription_plans WHERE id = $1"
            await connection.execute(query, plan_id)
        return jsonify({"message": "Subscription plan deleted successfully"}), 200
    except Exception as e:
        logging.error("Error deleting subscription plan: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# جلب جميع خطط الاشتراك، مع إمكانية التصفية حسب subscription_type_id
@admin_routes.route("/subscription-plans", methods=["GET"])
async def get_subscription_plans():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        subscription_type_id = request.args.get("subscription_type_id")
        async with current_app.db_pool.acquire() as connection:
            if subscription_type_id:
                query = """
                    SELECT id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at
                    FROM subscription_plans
                    WHERE subscription_type_id = $1
                    ORDER BY created_at DESC
                """
                results = await connection.fetch(query, int(subscription_type_id))
            else:
                query = """
                    SELECT id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at
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
async def get_subscription_plan(plan_id: int):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at
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
async def get_subscriptions():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        # الحصول على المتغيرات من استعلام URL
        user_id     = request.args.get("user_id")
        channel_id  = request.args.get("channel_id")
        status      = request.args.get("status")  # متوقع: active أو inactive
        start_date  = request.args.get("start_date")  # تنسيق (مثلاً: YYYY-MM-DD)
        end_date    = request.args.get("end_date")
        page        = int(request.args.get("page", 1))
        page_size   = int(request.args.get("page_size", 20))
        offset      = (page - 1) * page_size

        query = "SELECT * FROM subscriptions WHERE 1=1"
        params = []

        if user_id:
            query += f" AND user_id = ${len(params)+1}"
            params.append(user_id)
        if channel_id:
            query += f" AND channel_id = ${len(params)+1}"
            params.append(channel_id)
        if status:
            if status.lower() == "active":
                query += " AND is_active = true"
            elif status.lower() == "inactive":
                query += " AND is_active = false"
        if start_date:
            query += f" AND expiry_date >= ${len(params)+1}"
            params.append(start_date)
        if end_date:
            query += f" AND expiry_date <= ${len(params)+1}"
            params.append(end_date)
        # إضافة ترتيب وتجزئة النتائج
        query += f" ORDER BY expiry_date DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        params.append(page_size)
        params.append(offset)

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *params)

        return jsonify([dict(row) for row in rows])
    except Exception as e:
        logging.error("Error fetching subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# =====================================
# 2. API لجلب بيانات الدفعات مع دعم الفلاتر والتجزئة والتقارير المالية
# =====================================
@admin_routes.route("/payments", methods=["GET"])
async def get_payments():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        status     = request.args.get("status")
        user_id    = request.args.get("user_id")
        start_date = request.args.get("start_date")
        end_date   = request.args.get("end_date")
        page       = int(request.args.get("page", 1))
        page_size  = int(request.args.get("page_size", 20))
        offset     = (page - 1) * page_size

        query = "SELECT * FROM payments WHERE 1=1"
        params = []

        if status:
            query += f" AND status = ${len(params)+1}"
            params.append(status)
        if user_id:
            query += f" AND user_id = ${len(params)+1}"
            params.append(user_id)
        if start_date:
            query += f" AND payment_date >= ${len(params)+1}"
            params.append(start_date)
        if end_date:
            query += f" AND payment_date <= ${len(params)+1}"
            params.append(end_date)

        query += f" ORDER BY payment_date DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
        params.append(page_size)
        params.append(offset)

        async with current_app.db_pool.acquire() as connection:
            rows = await connection.fetch(query, *params)

        # تقارير مالية: إذا طلب الأدمن إجمالي الإيرادات عبر استخدام معامل report=total_revenue
        total_revenue = 0
        if request.args.get("report") == "total_revenue":
            rev_query = "SELECT SUM(amount) as total FROM payments WHERE 1=1"
            rev_params = []
            if status:
                rev_query += f" AND status = ${len(rev_params)+1}"
                rev_params.append(status)
            if user_id:
                rev_query += f" AND user_id = ${len(rev_params)+1}"
                rev_params.append(user_id)
            if start_date:
                rev_query += f" AND payment_date >= ${len(rev_params)+1}"
                rev_params.append(start_date)
            if end_date:
                rev_query += f" AND payment_date <= ${len(rev_params)+1}"
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

# =====================================
# 3. API لتعديل اشتراك مستخدم
# =====================================
@admin_routes.route("/subscriptions/<int:subscription_id>", methods=["PUT"])
async def update_subscription(subscription_id):
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        data = await request.get_json()
        # الحقول الممكن تحديثها: expiry_date, is_active, subscription_plan_id، ويمكن إضافة حقول أخرى حسب الحاجة
        expiry_date          = data.get("expiry_date")
        is_active            = data.get("is_active")
        subscription_plan_id = data.get("subscription_plan_id")
        source               = data.get("source")  # مثال على مصدر الاشتراك: تلقائي أو يدوي

        if expiry_date is None and is_active is None and subscription_plan_id is None and source is None:
            return jsonify({"error": "No fields provided for update"}), 400

        update_fields = []
        params = []
        idx = 1

        if expiry_date:
            update_fields.append(f"expiry_date = ${idx}")
            params.append(expiry_date)
            idx += 1
        if is_active is not None:
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

        # تحديث حقل updated_at تلقائياً
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
async def add_subscription():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        data = await request.get_json()
        # الحقول المطلوبة: channel_id, telegram_id, expiry_date, subscription_type_id, subscription_plan_id
        channel_id           = data.get("channel_id")
        telegram_id          = data.get("telegram_id")
        expiry_date          = data.get("expiry_date")
        subscription_type_id = data.get("subscription_type_id")
        subscription_plan_id = data.get("subscription_plan_id")
        user_id              = data.get("user_id")
        payment_id           = data.get("payment_id", None)  # إن وُجدت الدفعة المرتبطة
        is_active            = data.get("is_active", True)
        source               = data.get("source", "manual")  # مثلاً: يدوي أو تلقائي

        if not all([channel_id, telegram_id, expiry_date, subscription_type_id, subscription_plan_id]):
            return jsonify({"error": "Missing required fields"}), 400

        query = """
            INSERT INTO subscriptions 
            (user_id, expiry_date, is_active, channel_id, subscription_type_id, telegram_id, subscription_plan_id, payment_id, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *;
        """
        async with current_app.db_pool.acquire() as connection:
            row = await connection.fetchrow(
                query,
                user_id,
                expiry_date,
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