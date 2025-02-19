import os
import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import json




# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(
        **DATABASE_CONFIG,
        command_timeout=60,  # تأكيد عدم انتهاء المهلة بسرعة
        statement_cache_size=0  # تعطيل التخزين المؤقت للاستعلامات
    )
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
