
import json
import logging
from quart import Blueprint, request, jsonify, current_app
from config import DATABASE_CONFIG
import asyncpg
import json
from asyncpg.exceptions import DataError
from datetime import datetime
from decimal import Decimal
from utils.discount_utils import calculate_discounted_price
import asyncio

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
    """
    النسخة النهائية والمحسنة للأداء: تجلب كل البيانات اللازمة بأقل عدد من الاستعلامات
    ومتوافقة مع منطق عرض المقاعد الوهمية.
    """
    try:
        subscription_type_id = request.args.get("subscription_type_id", type=int)
        telegram_id = request.args.get("telegram_id", type=int)

        async with current_app.db_pool.acquire() as connection:
            # الخطوة 1: جلب الخطط
            params = []
            where_clauses = ["is_active = true"]
            if subscription_type_id:
                params.append(subscription_type_id)
                where_clauses.append(f"subscription_type_id = ${len(params)}")

            base_query = f"SELECT * FROM subscription_plans WHERE {' AND '.join(where_clauses)} ORDER BY price ASC;"
            base_plans = await connection.fetch(base_query, *params)

            if not base_plans:
                return jsonify([]), 200

            plan_ids = [p['id'] for p in base_plans]
            type_ids = list(set(p['subscription_type_id'] for p in base_plans))

            # الخطوة 2: جلب كل الخصومات المحتملة دفعة واحدة (مع تحديث is_tiered)
            all_discounts_query = """
                SELECT id, name, discount_type, discount_value, lock_in_price,
                       is_tiered, price_lock_duration_months,
                       applicable_to_subscription_plan_id, applicable_to_subscription_type_id
                FROM discounts
                WHERE (applicable_to_subscription_plan_id = ANY($1::int[]) OR applicable_to_subscription_type_id = ANY($2::int[]))
                  AND is_active = true AND target_audience = 'all_new'
                  AND (start_date IS NULL OR start_date <= NOW())
                  AND (end_date IS NULL OR end_date >= NOW())
            """
            all_discounts = await connection.fetch(all_discounts_query, plan_ids, type_ids)

            # الخطوة 3: جلب كل طبقات الخصم المتدرجة دفعة واحدة (مع تحديث is_tiered)
            tiered_discount_ids = [d['id'] for d in all_discounts if d['is_tiered']]
            all_tiers = {}
            if tiered_discount_ids:
                # SELECT * سيجلب الحقول الجديدة display_fake_count و fake_count_value
                tiers_query = "SELECT * FROM discount_tiers WHERE discount_id = ANY($1::int[]) ORDER BY discount_id, tier_order ASC"
                tier_records = await connection.fetch(tiers_query, tiered_discount_ids)
                for tier in tier_records:
                    if tier['discount_id'] not in all_tiers:
                        all_tiers[tier['discount_id']] = []
                    all_tiers[tier['discount_id']].append(dict(tier))

            # الخطوة 4: جلب كل الأسعار المثبتة للمستخدم دفعة واحدة
            locked_prices = {}
            if telegram_id:
                locked_price_query = """
                    SELECT subscription_plan_id, locked_price FROM user_discounts ud
                    JOIN users u ON u.id = ud.user_id
                    WHERE u.telegram_id = $1 AND ud.is_active = true AND ud.subscription_plan_id = ANY($2::int[])
                    AND (ud.expires_at IS NULL OR ud.expires_at > NOW()) -- التأكد من أن السعر لم تنته صلاحيته
                """
                locked_price_records = await connection.fetch(locked_price_query, telegram_id, plan_ids)
                locked_prices = {r['subscription_plan_id']: Decimal(r['locked_price']) for r in locked_price_records}

            # الخطوة 5: المعالجة في الذاكرة (سريع جداً)
            processed_plans = []
            for plan in base_plans:
                base_price = Decimal(plan['price'])
                possible_prices = [{'price': base_price, 'original_price': None, 'discount_details': {}}]

                if plan['id'] in locked_prices and locked_prices[plan['id']] < base_price:
                    possible_prices.append(
                        {'price': locked_prices[plan['id']], 'original_price': base_price,
                         'discount_details': {"discount_name": "Your Locked-in Price", "lock_in_price": True}})

                applicable_discounts = [d for d in all_discounts if
                                        d['applicable_to_subscription_plan_id'] == plan['id'] or
                                        d['applicable_to_subscription_type_id'] == plan['subscription_type_id']]

                for offer in applicable_discounts:
                    # ⭐ --- بداية المنطق المدمج للخصومات المتدرجة ---
                    if offer['is_tiered']:
                        tiers_for_offer = all_tiers.get(offer['id'], [])
                        active_tier = next(
                            (t for t in tiers_for_offer if t['is_active'] and t['used_slots'] < t['max_slots']), None)

                        if active_tier:
                            discounted_price = calculate_discounted_price(base_price, "percentage",
                                                                          active_tier['discount_value'])
                            next_tier = next(
                                (t for t in tiers_for_offer if t['tier_order'] > active_tier['tier_order']), None)

                            # ⭐ منطق عرض المقاعد (الحقيقي أو الوهمي)
                            real_remaining = active_tier['max_slots'] - active_tier['used_slots']
                            display_slots = real_remaining
                            if active_tier.get('display_fake_count') and active_tier.get(
                                    'fake_count_value') is not None:
                                # عرض القيمة الأقل بين المتبقي الحقيقي والوهمي
                                display_slots = min(real_remaining, active_tier['fake_count_value'])

                            tier_info = {
                                "is_tiered": True,
                                "remaining_slots": max(0, display_slots),  # ضمان عدم عرض قيمة سالبة
                                "has_limited_slots": True,
                                "next_tier_info": None
                            }
                            if next_tier:
                                next_price = calculate_discounted_price(base_price, "percentage",
                                                                        next_tier['discount_value'])
                                tier_info['next_tier_info'] = {
                                    # ⭐ رسالة المستوى التالي المحدثة
                                    "message": f"بعد انتهاء هذه المقاعد سيزيد السعر ليصبح ${next_price:.2f}"
                                }

                            discount_details = {
                                "discount_id": offer['id'],
                                "discount_name": offer['name'],
                                "lock_in_price": offer['lock_in_price'],
                                "price_lock_duration_months": offer['price_lock_duration_months'],
                                **tier_info
                            }
                            possible_prices.append({'price': discounted_price, 'original_price': base_price,
                                                    'discount_details': discount_details})
                    # --- نهاية المنطق المدمج للخصومات المتدرجة ---
                    else:
                        # منطق الخصم العادي يبقى كما هو
                        discounted_price = calculate_discounted_price(base_price, offer['discount_type'],
                                                                      offer['discount_value'])
                        if discounted_price < base_price:
                            discount_details = {
                                "discount_id": offer['id'],
                                "discount_name": offer['name'],
                                "lock_in_price": offer['lock_in_price'],
                                "is_tiered": False
                            }
                            possible_prices.append({'price': discounted_price, 'original_price': base_price,
                                                    'discount_details': discount_details})

                best_price_option = min(possible_prices, key=lambda x: x['price'])

                final_plan_data = dict(plan)
                final_plan_data['price'] = f"{best_price_option['price']:.2f}"
                final_plan_data['original_price'] = f"{best_price_option['original_price']:.2f}" if best_price_option[
                    'original_price'] else None
                final_plan_data['discount_details'] = best_price_option['discount_details']
                processed_plans.append(final_plan_data)

        return jsonify(processed_plans), 200

    except Exception as e:
        logging.error(f"Error fetching public subscription plans: {e}", exc_info=True)
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


@public_routes.route("/subscriptions/all", methods=["GET"])
async def get_all_public_subscriptions():
    """
    تجلب هذه النقطة جميع مجموعات وأنواع وخطط الاشتراكات النشطة في بنية متداخلة.
    يمكن تمرير 'telegram_id' كمعامل اختياري للحصول على الخصومات الخاصة بالمستخدم.
    """
    telegram_id = request.args.get("telegram_id", type=int)

    try:
        async with current_app.db_pool.acquire() as connection:
            # تم تصحيح الاستعلام لاستخدام TO_CHAR بدلاً من FORMAT
            comprehensive_query = """
            WITH plans_with_discounts AS (
                -- الخطوة 1: حساب الأسعار النهائية لجميع الخطط مع مراعاة الخصومات
                SELECT
                    p.id, p.subscription_type_id, p.name, p.duration_days, p.price, p.is_active,

                    CASE 
                        WHEN ud.locked_price IS NOT NULL THEN p.price
                        WHEN d.id IS NOT NULL THEN p.price
                        ELSE NULL
                    END AS original_price,

                    COALESCE(
                        ud.locked_price,
                        CASE
                            WHEN d.discount_type = 'percentage' THEN p.price * (1 - (d.discount_value / 100))
                            WHEN d.discount_type = 'fixed_amount' THEN p.price - d.discount_value
                        END,
                        p.price
                    ) AS final_price

                FROM subscription_plans p

                LEFT JOIN users u ON u.telegram_id = $1
                LEFT JOIN user_discounts ud ON ud.user_id = u.id AND ud.subscription_plan_id = p.id AND ud.is_active = TRUE

                LEFT JOIN LATERAL (
                    SELECT * FROM discounts
                    WHERE 
                        ud.id IS NULL
                        AND (discounts.applicable_to_subscription_plan_id = p.id OR discounts.applicable_to_subscription_type_id = p.subscription_type_id)
                        AND discounts.is_active = TRUE AND discounts.target_audience = 'all_new'
                        AND (discounts.start_date IS NULL OR discounts.start_date <= NOW())
                        AND (discounts.end_date IS NULL OR discounts.end_date >= NOW())
                    ORDER BY 
                        CASE WHEN discounts.applicable_to_subscription_plan_id IS NOT NULL THEN 0 ELSE 1 END,
                        discounts.created_at DESC
                    LIMIT 1
                ) d ON TRUE

                WHERE p.is_active = TRUE
            )
            -- الخطوة 2: تجميع الخطط داخل الأنواع، والأنواع داخل المجموعات
            SELECT
                g.id, g.name, g.description, g.image_url, g.color, g.icon, g.sort_order,
                COALESCE(json_agg(
                    json_build_object(
                        'id', st.id,
                        'name', st.name,
                        'channel_id', st.channel_id,
                        'description', st.description,
                        'image_url', st.image_url,
                        'features', COALESCE(st.features, '[]'::jsonb),
                        'usp', st.usp,
                        'is_recommended', st.is_recommended,
                        'terms_and_conditions', COALESCE(st.terms_and_conditions, '[]'::jsonb),
                        'sort_order', st.sort_order,
                        'subscription_plans', st.plans
                    ) ORDER BY st.sort_order ASC, st.created_at DESC
                ) FILTER (WHERE st.id IS NOT NULL), '[]'::json) AS subscription_types

            FROM subscription_groups g

            LEFT JOIN (
                SELECT
                    st_inner.id, st_inner.group_id, st_inner.name, st_inner.channel_id, st_inner.description,
                    st_inner.image_url, st_inner.features, st_inner.usp, st_inner.is_recommended,
                    st_inner.terms_and_conditions, st_inner.sort_order, st_inner.created_at,
                    COALESCE(json_agg(
                        json_build_object(
                            'id', p.id,
                            'name', p.name,
                            'duration_days', p.duration_days,
                            -- <<< التغيير هنا: استخدام TO_CHAR للتنسيق الصحيح في PostgreSQL
                            'price', TO_CHAR(p.final_price, 'FM999999999.00'),
                            'original_price', CASE WHEN p.original_price IS NOT NULL THEN TO_CHAR(p.original_price, 'FM999999999.00') ELSE NULL END
                        ) ORDER BY p.final_price ASC
                    ) FILTER (WHERE p.id IS NOT NULL), '[]'::json) AS plans
                FROM subscription_types st_inner
                LEFT JOIN plans_with_discounts p ON p.subscription_type_id = st_inner.id
                WHERE st_inner.is_active = TRUE
                GROUP BY st_inner.id
            ) st ON st.group_id = g.id

            WHERE g.is_active = TRUE
            GROUP BY g.id
            ORDER BY g.sort_order ASC, g.name ASC;
            """

            results = await connection.fetch(comprehensive_query, telegram_id)

        final_structure = [
            {**row, "subscription_types": json.loads(row["subscription_types"])}
            for row in results
        ]

        return jsonify(final_structure), 200, {
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/json; charset=utf-8"
        }

    except Exception as e:
        logging.error("Error fetching all public subscriptions: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500