# payment_confirmation.py
import logging
from quart import Blueprint, request, jsonify
import json  # استيراد مكتبة json

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    ✅ نقطة API مُدمجة لتأكيد استلام الدفع ومعالجة بيانات المستخدم (بدلاً من نقطتين منفصلتين).
    تقوم بتسجيل بيانات الدفع والمستخدم كـ 'بانتظار التأكيد' وإرجاع استجابة بنجاح.
    """
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment (مدمجة): {json.dumps(data, indent=2)}")

        tx_hash = data.get("txHash")
        plan_id = data.get("planId")
        telegram_id = data.get("telegramId")
        telegram_username = data.get("telegramUsername") # ✅ استلام اسم المستخدم
        full_name = data.get("fullName") # ✅ استلام الاسم الكامل

        if not all([tx_hash, plan_id, telegram_id]): # ✅ التحقق من البيانات الأساسية (tx_hash, plan_id, telegram_id)
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(f"✅ استلام طلب تأكيد الدفع (مدمج): txHash={tx_hash}, planId={plan_id}, telegram_id={telegram_id}, username={telegram_username}, full_name={full_name}")

        # ✅ في التصميم الحالي، نقوم فقط بتسجيل جميع البيانات المستلمة كـ 'بانتظار التأكيد' في سجلات الخادم
        logging.info(f"💾 تسجيل بيانات الدفع والمستخدم كـ 'بانتظار التأكيد' (وهمي): txHash={tx_hash}, planId={plan_id}, telegram_id={telegram_id}, username={telegram_username}, full_name={full_name}")

        return jsonify({"message": "Payment confirmation and user data received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (مدمجة): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500