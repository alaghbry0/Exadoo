import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import asyncpg
from asyncpg.exceptions import DataError
from datetime import datetime
from decimal import Decimal
from utils.discount_utils import calculate_discounted_price

# وظيفة لإنشاء اتصال بقاعدة البيانات
async def create_db_pool():
    return await asyncpg.create_pool(**DATABASE_CONFIG)


# إنشاء Blueprint للواجهة العامة تحت مسار /api/public
public_routes = Blueprint("public_routes", __name__, url_prefix="/api/public")


# --- إضافة جديدة: نقطة API لجلب مجموعات الاشتراكات العامة ---
@public_routes.route("/subscription-groups", methods=["GET"])
async def get_public_subscription_groups():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    id, 
                    name, 
                    description, 
                    image_url, 
                    color, 
                    icon, 
                    is_active,
                    sort_order,
                    created_at,
                    updated_at
                FROM subscription_groups
                WHERE is_active = true
                ORDER BY sort_order ASC, name ASC
            """
            results = await connection.fetch(query)

        groups = [dict(row) for row in results]
        return jsonify(groups), 200, {
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/json; charset=utf-8"
        }
    except Exception as e:
        logging.error("Error fetching public subscription groups: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# نقطة API لجلب قائمة بأنواع الاشتراكات العامة
@public_routes.route("/subscription-types", methods=["GET"])
async def get_public_subscription_types():
    try:
        # --- إضافة جديدة: استقبال group_id كمعامل اختياري ---
        group_id_filter = request.args.get("group_id", type=int)

        async with current_app.db_pool.acquire() as connection:
            params = []
            where_clauses = ["st.is_active = true"]

            if group_id_filter is not None:
                params.append(group_id_filter)
                where_clauses.append(f"st.group_id = ${len(params)}")

            where_sql = " AND ".join(where_clauses)

            query = f"""
                SELECT 
                    st.id, 
                    st.name, 
                    st.channel_id, 
                    st.description, 
                    st.image_url, 
                    st.features, 
                    st.usp, 
                    st.is_active,
                    st.is_recommended,
                    st.terms_and_conditions,
                    st.group_id,        -- <-- إضافة group_id هنا
                    st.sort_order,      -- <-- إضافة sort_order هنا
                    st.created_at
                FROM subscription_types st
                WHERE {where_sql}
                ORDER BY st.sort_order ASC, st.created_at DESC 
            """  # تم تعديل الترتيب ليشمل sort_order أولاً

            results = await connection.fetch(query, *params)

        types = []
        for row in results:
            row_dict = dict(row)
            if isinstance(row_dict.get("features"), str):
                row_dict["features"] = json.loads(row_dict["features"]) if row_dict["features"] else []
            elif row_dict.get("features") is None:
                row_dict["features"] = []

            if isinstance(row_dict.get("terms_and_conditions"), str):
                row_dict["terms_and_conditions"] = json.loads(row_dict["terms_and_conditions"]) if row_dict[
                    "terms_and_conditions"] else []
            elif row_dict.get("terms_and_conditions") is None:
                row_dict["terms_and_conditions"] = []

            types.append(row_dict)

        return jsonify(types), 200, {
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/json; charset=utf-8"
        }
    except Exception as e:
        logging.error("Error fetching public subscription types: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@public_routes.route("/subscription-plans", methods=["GET"])
async def get_public_subscription_plans():
    try:
        subscription_type_id = request.args.get("subscription_type_id", type=int)
        telegram_id = request.args.get("telegram_id", type=int)

        if not subscription_type_id:
            return jsonify({"error": "subscription_type_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            base_query = """
                SELECT *
                FROM subscription_plans
                WHERE subscription_type_id = $1 AND is_active = true
                ORDER BY price ASC;
            """
            base_plans_records = await connection.fetch(base_query, subscription_type_id)

            # قائمة لتخزين القواميس النهائية بعد المعالجة
            processed_plans = []

            for plan_record in base_plans_records:
                # <<< تعديل 1: تحويل صريح للقيم التي سنستخدمها في الحسابات إلى Decimal
                # هذا يحل تحذير "Expected type 'Decimal', got 'str' instead"
                current_price = Decimal(plan_record['price'])
                original_price = None  # نبدأ بـ None

                if telegram_id:
                    # أ. تحقق من وجود سعر مُثبّت (Locked Price)
                    locked_price_query = """
                        SELECT locked_price FROM user_discounts ud
                        JOIN users u ON u.id = ud.user_id
                        WHERE u.telegram_id = $1 AND ud.subscription_plan_id = $2 AND ud.is_active = true
                    """
                    locked_record = await connection.fetchrow(locked_price_query, telegram_id, plan_record['id'])

                    if locked_record:
                        original_price = current_price
                        current_price = Decimal(locked_record['locked_price'])
                    else:
                        # ب. تحقق من وجود عروض عامة
                        public_offer_query = """
                            SELECT discount_type, discount_value FROM discounts
                            WHERE 
                                (applicable_to_subscription_plan_id = $1 OR applicable_to_subscription_type_id = $2)
                                AND is_active = true AND target_audience = 'all_new'
                                AND (start_date IS NULL OR start_date <= NOW())
                                AND (end_date IS NULL OR end_date >= NOW())
                            ORDER BY 
                                CASE WHEN applicable_to_subscription_plan_id IS NOT NULL THEN 0 ELSE 1 END,
                                created_at DESC 
                            LIMIT 1;
                        """
                        offer_record = await connection.fetchrow(public_offer_query, plan_record['id'],
                                                                 plan_record['subscription_type_id'])

                        if offer_record:
                            # تحويل صريح لقيمة الخصم لضمان النوع الصحيح
                            discount_value = Decimal(offer_record['discount_value'])

                            discounted_price = calculate_discounted_price(
                                current_price,
                                offer_record['discount_type'],
                                discount_value
                            )
                            if discounted_price < current_price:
                                original_price = current_price
                                current_price = discounted_price

                # <<< تعديل 2: بناء القاموس النهائي هنا
                # هذا يحل تحذيرات "got 'Decimal'/'None' instead of 'str'"
                # لأننا نبني قاموسا نظيفا في النهاية بدلا من تعديل القاموس الأصلي
                final_plan_data = dict(plan_record)  # نسخ كل البيانات الأصلية
                final_plan_data['price'] = f"{current_price:.2f}"
                final_plan_data['original_price'] = f"{original_price:.2f}" if original_price is not None else None

                processed_plans.append(final_plan_data)

        return jsonify(processed_plans), 200

    except Exception as e:
        logging.error("Error fetching public subscription plans: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@public_routes.route("/payment-history", methods=["GET"])
async def get_payment_history():
    try:
        telegram_id = request.args.get("telegram_id")
        offset = request.args.get("offset", "0")
        limit = request.args.get("limit", "10")

        if not telegram_id:
            return jsonify({"error": "telegram_id is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    p.tx_hash, 
                    p.amount_received, 
                    p.subscription_plan_id, 
                    p.status, 
                    p.processed_at, 
                    p.payment_token, 
                    p.error_message,
                    sp.name AS plan_name,
                    st.name AS subscription_name
                FROM payments p
                JOIN subscription_plans sp ON p.subscription_plan_id = sp.id
                JOIN subscription_types st ON sp.subscription_type_id = st.id
                WHERE p.telegram_id = $1 AND p.status IN ('completed', 'failed')
                ORDER BY p.id DESC
                OFFSET $2 LIMIT $3;
            """
            results = await connection.fetch(query, int(telegram_id), int(offset), int(limit))

        payments = [dict(r) for r in results]
        return (
            jsonify(payments),
            200,
            {
                "Cache-Control": "public, max-age=300",
                "Content-Type": "application/json; charset=utf-8"
            }
        )
    except Exception as e:
        logging.error("Error fetching payment history: %s", e, exc_info=True)
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


@public_routes.route("/terms-conditions", methods=["GET"])
async def get_public_terms_conditions():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT terms_array, updated_at
                FROM terms_conditions
                ORDER BY updated_at DESC
                LIMIT 1;
            """
            result = await connection.fetchrow(query)

            if result:
                return jsonify({
                    "terms_array": result["terms_array"],
                    "updated_at": result["updated_at"].isoformat() if result["updated_at"] else None
                }), 200
            else:
                return jsonify({"terms_array": []}), 200

    except Exception as e:
        logging.error("Error fetching public terms and conditions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500




