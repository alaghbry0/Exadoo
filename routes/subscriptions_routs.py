import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG


async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# إنشاء Blueprint للواجهة العامة تحت مسار /api/public
public_routes = Blueprint("public_routes", __name__, url_prefix="/api/public")


# نقطة API لجلب قائمة بأنواع الاشتراكات العامة
@public_routes.route("/subscription-types", methods=["GET"])
async def get_public_subscription_types():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT id, name, channel_id, description, image_url, features, usp, is_active, created_at
                FROM subscription_types
                WHERE is_active = true
                ORDER BY created_at DESC
            """
            results = await connection.fetch(query)

        types = []
        for row in results:
            row_dict = dict(row)
            # التأكد من تحويل الحقل features إلى مصفوفة JSON
            row_dict["features"] = json.loads(row_dict["features"]) if row_dict["features"] else []
            types.append(row_dict)

        # تضمين رؤوس للتخزين المؤقت لتحسين الأداء
        return jsonify(types), 200, {
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/json; charset=utf-8"
        }
    except Exception as e:
        logging.error("Error fetching public subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# نقطة API لجلب قائمة بخطط الاشتراك العامة
@public_routes.route("/subscription-plans", methods=["GET"])
async def get_public_subscription_plans():
    try:
        subscription_type_id = request.args.get("subscription_type_id")
        async with current_app.db_pool.acquire() as connection:
            if subscription_type_id:
                query = """
                    SELECT id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at
                    FROM subscription_plans
                    WHERE subscription_type_id = $1 AND is_active = true
                    ORDER BY created_at DESC
                """
                results = await connection.fetch(query, int(subscription_type_id))
            else:
                query = """
                    SELECT id, subscription_type_id, name, price, telegram_stars_price, duration_days, is_active, created_at
                    FROM subscription_plans
                    WHERE is_active = true
                    ORDER BY created_at DESC
                """
                results = await connection.fetch(query)

        plans = [dict(r) for r in results]
        return jsonify(plans), 200, {
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/json; charset=utf-8"
        }
    except Exception as e:
        logging.error("Error fetching public subscription plans: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
