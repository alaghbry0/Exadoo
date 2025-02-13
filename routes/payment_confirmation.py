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
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")

        # التحقق من البيانات الأساسية
        if not all([payment_id, plan_id_str, telegram_id_str]):
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(
            f"✅ استلام طلب تأكيد الدفع (مدمج): paymentId={payment_id}, planId={plan_id_str}, "
            f"telegram_id={telegram_id_str}, username={telegram_username}, full_name={full_name}"
        )

        amount = 0
        telegram_id = int(telegram_id_str)

        try:
            subscription_type_id = int(plan_id_str)
        except ValueError:
            subscription_type_id = 3
            logging.warning(f"⚠️ Plan ID '{plan_id_str}' is not a valid integer. Using default subscription type ID: {subscription_type_id}")
        except TypeError:
            subscription_type_id = 3
            logging.warning(f"⚠️ Plan ID is missing in request. Using default subscription type ID: {subscription_type_id}")

        # استخدام current_app.db_pool وتمرير username و full_name إلى record_payment
        async with current_app.db_pool.acquire() as conn:
            await record_payment(conn, telegram_id, payment_id, amount, subscription_type_id, username=telegram_username, full_name=full_name) # ✅ تمرير username و full_name

        logging.info(
            f"💾 تسجيل بيانات الدفع والمستخدم كدفعة معلقة: paymentId={payment_id}, "
            f"planId={plan_id_str}, telegram_id={telegram_id}, subscription_type_id={subscription_type_id}, username={telegram_username}, full_name={full_name}"
        )

        return jsonify({"message": "Payment confirmation and user data received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (مدمجة): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500