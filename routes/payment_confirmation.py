# payment_confirmation.py
import logging
from quart import Blueprint, request, jsonify
import json  # استيراد مكتبة json
from database.db_queries import (record_payment)

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API مُدمجة لتأكيد استلام الدفع ومعالجة بيانات المستخدم.
    تستقبل هذه النقطة بيانات الدفع بما في ذلك paymentId (المعرف الفريد) وتقوم بتسجيل
    بيانات الدفع والمستخدم كدفعة معلقة.
    """
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment (مدمجة): {json.dumps(data, indent=2)}")

        # استلام paymentId بدلاً من txHash
        payment_id = data.get("paymentId")
        plan_id = data.get("planId")
        telegram_id = data.get("telegramId")
        telegram_username = data.get("telegramUsername")  # استلام اسم المستخدم
        full_name = data.get("fullName")  # استلام الاسم الكامل

        # التحقق من البيانات الأساسية
        if not all([payment_id, plan_id, telegram_id]):
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(
            f"✅ استلام طلب تأكيد الدفع (مدمج): paymentId={payment_id}, planId={plan_id}, "
            f"telegram_id={telegram_id}, username={telegram_username}, full_name={full_name}"
        )

        await record_payment(conn, user_id, payment_id, amount, subscription_type_id)

        logging.info(
            f"💾 تسجيل بيانات الدفع والمستخدم كدفعة معلقة: paymentId={payment_id}, "
            f"planId={plan_id}, telegram_id={telegram_id}, username={telegram_username}, full_name={full_name}"
        )

        return jsonify({"message": "Payment confirmation and user data received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (مدمجة): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
