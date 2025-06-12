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


@public_routes.route("/subscription-groups", methods=["GET"])
async def get_public_subscription_groups():
    try:
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT 
                    sg.id, 
                    sg.name, 
                    sg.description, 
                    sg.image_url, 
                    sg.color, 
                    sg.icon, 
                    sg.is_active,
                    sg.sort_order,
                    sg.display_as_single_card, -- <--- الحقل الجديد
                    sg.created_at,
                    sg.updated_at
                FROM subscription_groups sg -- تمت إضافة sg كاسم مستعار
                WHERE sg.is_active = true  -- استخدام الاسم المستعار هنا أيضاً
                ORDER BY sg.sort_order ASC, sg.name ASC
            """
            results = await connection.fetch(query)

        groups = [dict(row) for row in results]
        return jsonify(groups), 200, {
            "Cache-Control": "public, max-age=300", # أو أزله إذا كانت البيانات تتغير كثيرًا
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



# دالة مساعدة عامة لمعالجة بيانات نوع الاشتراك (يمكن تكييفها من دالة لوحة التحكم)
def _format_public_subscription_type(type_data, plans_for_type):
    data = dict(type_data)  # type_data ستكون Record من fetch

    # معالجة features إذا كانت سلسلة JSON
    if 'features' in data and isinstance(data['features'], str):
        data['features'] = json.loads(data['features'] or "[]")
    elif 'features' not in data or data['features'] is None:
        data['features'] = []

    # معالجة terms_and_conditions إذا كانت سلسلة JSON
    if 'terms_and_conditions' in data and isinstance(data['terms_and_conditions'], str):
        data['terms_and_conditions'] = json.loads(data['terms_and_conditions'] or "[]")
    elif 'terms_and_conditions' not in data or data['terms_and_conditions'] is None:
        data['terms_and_conditions'] = []

    data['subscription_options'] = [dict(plan) for plan in plans_for_type]  # أو 'plans'
    return data


@public_routes.route("/all-subscription-data", methods=["GET"])
async def get_public_all_subscription_data():
    try:
        async with current_app.db_pool.acquire() as connection:
            # 1. جلب المجموعات النشطة
            groups_query = """
                SELECT 
                    id, name, description, image_url, color, icon, 
                    sort_order, display_as_single_card
                FROM subscription_groups
                WHERE is_active = TRUE
                ORDER BY sort_order;
            """
            groups_records = await connection.fetch(groups_query)

            # 2. جلب كل أنواع الاشتراكات النشطة
            types_query = """
                SELECT 
                    id, name, channel_id, group_id, sort_order,
                    description, image_url, features, usp, is_recommended, 
                    terms_and_conditions, created_at
                FROM subscription_types
                WHERE is_active = TRUE
                ORDER BY group_id, sort_order, created_at DESC; 
            """
            types_records = await connection.fetch(types_query)

            # 3. جلب كل خطط الاشتراكات النشطة
            plans_query = """
                SELECT 
                    id, subscription_type_id, name, price, original_price, 
                    telegram_stars_price, duration_days
                FROM subscription_plans
                WHERE is_active = TRUE
                ORDER BY subscription_type_id, price ASC;
            """
            plans_records = await connection.fetch(plans_query)

        # هيكلة البيانات:
        # تحويل الخطط إلى قاموس لتسهيل البحث
        plans_by_type_id = {}
        for plan_row in plans_records:
            plan_dict = dict(plan_row)
            type_id = plan_dict['subscription_type_id']
            if type_id not in plans_by_type_id:
                plans_by_type_id[type_id] = []
            plans_by_type_id[type_id].append(plan_dict)

        # تحويل الأنواع إلى قاموس لتسهيل البحث وتضمين الخطط
        types_by_group_id = {}
        ungrouped_types = []

        for type_row in types_records:
            type_dict = dict(type_row)
            type_id = type_dict['id']
            group_id = type_dict.get('group_id')

            # استخدام دالة التنسيق لتجهيز النوع وإضافة الخطط
            formatted_type = _format_public_subscription_type(
                type_row,  # تمرير السجل مباشرة
                plans_by_type_id.get(type_id, [])
            )

            if group_id:
                if group_id not in types_by_group_id:
                    types_by_group_id[group_id] = []
                types_by_group_id[group_id].append(formatted_type)
            else:
                ungrouped_types.append(formatted_type)

        # بناء الاستجابة النهائية
        response_data = []
        for group_row in groups_records:
            group_data = dict(group_row)
            group_id = group_data['id']
            group_data['subscription_types'] = types_by_group_id.get(group_id, [])
            response_data.append(group_data)

        # إضافة الأنواع غير المجمعة (إذا أردت عرضها بشكل منفصل)
        if ungrouped_types:
            response_data.append({
                "id": None,  # أو معرف خاص للأنواع غير المجمعة
                "name": "اشتراكات أخرى",  # "Other Subscriptions"
                "description": "أنواع اشتراكات غير مخصصة لمجموعة",
                "display_as_single_card": False,  # الأنواع غير المجمعة تعرض منفصلة دائمًا
                "subscription_types": ungrouped_types,
                "sort_order": 999  # لضمان ظهورها في الآخر
            })

        # يمكنك فلترة المجموعات التي ليس لديها أنواع اشتراكات إذا أردت
        # response_data = [group for group in response_data if group['subscription_types']]

        return jsonify(response_data), 200, {
            "Cache-Control": "public, max-age=120",  # قلل max-age إذا كانت البيانات تتغير بسرعة
            "Content-Type": "application/json; charset=utf-8"
        }

    except Exception as e:
        logging.error("Error fetching all public subscription data: %s", e, exc_info=True)
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




