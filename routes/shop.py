from quart import Blueprint, jsonify, current_app
import logging

shop = Blueprint("shop", __name__)

@shop.route("/api/shop", methods=["GET"])
async def get_subscriptions():
    """
    🔹 إرجاع خطط الاشتراك المتاحة من قاعدة البيانات
    ✅ جلب البيانات من جدول `subscription_types`
    ✅ إعادة تنسيق البيانات إلى JSON
    """
    try:
        async with current_app.db_pool.acquire() as conn:
            plans = await conn.fetch(
                "SELECT id, name, price, details FROM subscription_types WHERE is_active = TRUE"
            )

        return jsonify([
            {
                "id": plan["id"],
                "name": plan["name"],
                "price": plan["price"],
                "details": plan["details"]
            }
            for plan in plans
        ]), 200

    except Exception as e:
        logging.error(f"❌ Error fetching subscription plans: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
