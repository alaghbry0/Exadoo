import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import asyncpg
from asyncpg.exceptions import DataError
from datetime import datetime


# وظيفة لإنشاء اتصال بقاعدة البيانات
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
                SELECT id, 
                    name, 
                    channel_id, 
                    description, 
                    image_url, 
                    features, 
                    usp, 
                    is_active,
                    is_recommended, -- أضف هذا الحقل الجديد
                    created_at
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
    

@public_routes.route("/wallet", methods=["GET"])
async def get_public_wallet():
    try:
        async with current_app.db_pool.acquire() as connection:
            wallet = await connection.fetchrow("SELECT wallet_address FROM wallet ORDER BY id DESC LIMIT 1")
        if wallet:
            return jsonify({"wallet_address": wallet["wallet_address"]}), 200
        else:
            return jsonify({"wallet_address": ""}), 200
    except Exception as e:
        logging.error("❌ Error fetching public wallet address: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@public_routes.route("/user/payments", methods=["GET"])
async def get_user_payment_history():
    try:
        # التحقق من المعلمات المطلوبة
        telegram_id = request.args.get('telegram_id')
        if not telegram_id:
            return jsonify({"error": "المعلمة المطلوبة telegram_id مفقودة"}), 400

        # معلمات التقسيم الصفحي
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 10)), 50)
        offset = (page - 1) * page_size

        # بناء الاستعلام
        query = '''
            SELECT 
                p.payment_token,
                p.amount_received,
                p.status,
                p.error_message,
                p.created_at,
                p.tx-hash,
                sp.name as plan_name,
                sp.duration_days,
                (SELECT COUNT(*) FROM payments WHERE telegram_id = $1) as total_count
            FROM payments p
            JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
            WHERE p.telegram_id = $1
            ORDER BY p.created_at DESC
            LIMIT $2 OFFSET $3
        '''

        async with current_app.db_pool.acquire() as conn:
            records = await conn.fetch(query, int(telegram_id), page_size, offset)
            total_count = records[0]['total_count'] if records else 0

            payments = []
            for record in records:
                payment = dict(record)
                payment.pop('total_count', None)
                if payment['tx-hash']:
                    payment['explorer_url'] = f"https://tonscan.org/tx/{payment['tx-hash']}"
                payment['created_at'] = payment['created_at'].isoformat()
                payments.append(payment)

            return jsonify({
                "results": payments,
                "pagination": {
                    "current_page": page,
                    "total_pages": (total_count + page_size - 1) // page_size,
                    "total_items": total_count
                }
            }), 200

    except Exception as e:
        logging.error(f"خطأ في جلب البيانات: {str(e)}")
        return jsonify({"error": "خطأ في الخادم"}), 500
