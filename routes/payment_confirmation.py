# payment_confirmation.py
import logging
from quart import Blueprint, request, jsonify
import json  # استيراد مكتبة json

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API لتأكيد استلام الدفع من الواجهة الأمامية (تصميم مثالي مبسط).
    تقوم بتسجيل الدفع كـ 'بانتظار التأكيد' وإرجاع استجابة بنجاح.
    """
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}")

        tx_hash = data.get("txHash")
        plan_id = data.get("planId")
        telegram_id = data.get("telegramId")

        if not all([tx_hash, plan_id, telegram_id]):
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(f"✅ استلام طلب تأكيد الدفع (تصميم مثالي مبسط): txHash={tx_hash}, planId={plan_id}, telegram_id={telegram_id}")

        # ✅ في التصميم المثالي المبسط، نقوم فقط بتسجيل الدفع كـ 'بانتظار التأكيد' في سجلات الخادم
        logging.info(f"💾 تسجيل الدفع كـ 'بانتظار التأكيد' (وهمي): txHash={tx_hash}, planId={plan_id}, telegram_id={telegram_id}")

        return jsonify({"message": "Payment confirmation received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (تصميم مثالي مبسط): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500