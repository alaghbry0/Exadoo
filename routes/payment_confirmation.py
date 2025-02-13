# payment_confirmation.py
import logging
from quart import Blueprint, request, jsonify, current_app
import json  # استيراد مكتبة json
from database.db_queries import record_payment

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API مُدمجة لتأكيد استلام الدفع ومعالجة بيانات المستخدم.
    """

    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment (مدمجة): {json.dumps(data, indent=2)}")

        # استلام البيانات
        payment_id = data.get("paymentId")
        plan_id_str = data.get("planId") # استلام planId كسلسلة نصية
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")

        # التحقق من البيانات الأساسية
        if not all([payment_id, plan_id_str, telegram_id_str]): # استخدام plan_id_str للتحقق
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(
            f"✅ استلام طلب تأكيد الدفع (مدمج): paymentId={payment_id}, planId={plan_id_str}, " # استخدام plan_id_str للتسجيل
            f"telegram_id={telegram_id_str}, username={telegram_username}, full_name={full_name}"
        )

        amount = 0  # قيمة افتراضية للمبلغ - يجب تحديد المصدر لاحقًا
        telegram_id = int(telegram_id_str) # تحويل telegram_id إلى عدد صحيح

        # ✅ Temporary hardcoded mapping for subscription_type_id based on planId string
        if plan_id_str == "premium_plan":
            subscription_type_id = 1  # Replace with your actual premium plan ID
        elif plan_id_str == "basic_plan":
            subscription_type_id = 2  # Replace with your actual basic plan ID
        else:
            subscription_type_id = 3  # Default or error case - adjust as needed
            logging.warning(f"⚠️ Plan ID '{plan_id_str}' not recognized. Using default subscription type ID: {subscription_type_id}")


        # استخدام current_app.db_pool
        async with current_app.db_pool.acquire() as conn:
            await record_payment(conn, telegram_id, payment_id, amount, subscription_type_id)

        logging.info(
            f"💾 تسجيل بيانات الدفع والمستخدم كدفعة معلقة: paymentId={payment_id}, "
            f"planId={plan_id_str}, telegram_id={telegram_id}, subscription_type_id={subscription_type_id}, username={telegram_username}, full_name={full_name}" # تسجيل subscription_type_id
        )

        return jsonify({"message": "Payment confirmation and user data received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (مدمجة): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500