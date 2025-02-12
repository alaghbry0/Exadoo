# webhook.py (تصميم مثالي مبسط)
import logging
import os
import aiohttp
from quart import Blueprint, request, jsonify, current_app
import json  # استيراد مكتبة json

webhook_bp = Blueprint("webhook", __name__)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL", "http://localhost:5000/api/subscribe")

@webhook_bp.route("/api/webhook", methods=["POST"])
async def webhook():
    """
    نقطة API لاستقبال إشعارات الدفع من TonAPI (تصميم مثالي مبسط).
    Webhook هو المصدر الموثوق لتفعيل الاشتراك.
    """
    try:
        # ✅ التحقق من WEBHOOK_SECRET
        secret = request.headers.get("Authorization")
        if not secret or secret != f"Bearer {WEBHOOK_SECRET}":
            logging.warning("❌ Unauthorized webhook request: Invalid or missing WEBHOOK_SECRET")
            return jsonify({"error": "Unauthorized request"}), 403

        # ✅ استقبال البيانات
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/webhook: {json.dumps(data, indent=2)}")

        # ✅ استخراج تفاصيل الدفع
        event = data.get("event")
        if event != "transaction_received":
            logging.info(f"⚠️ تجاهل حدث غير متعلق بالدفع: {event}")
            return jsonify({"message": "Event ignored"}), 200

        transaction = data.get("data", {})
        transaction_id = transaction.get("tx_hash")
        sender_address = transaction.get("sender", {}).get("address")
        recipient_address = transaction.get("recipient", {}).get("address")
        amount = transaction.get("amount", 0)
        status = transaction.get("status")

        # ✅ بيانات المستخدم والاشتراك (وهمية - سيتم تحسينها لاحقًا)
        telegram_id = 123456789  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
        subscription_type_id = 1  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
        username = "test_user"  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
        full_name = "Test User"  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة

        # ✅ التحقق من البيانات المطلوبة
        if not all([transaction_id, sender_address, recipient_address, amount, status, telegram_id, subscription_type_id]):
            logging.error("❌ بيانات الدفع غير مكتملة!")
            return jsonify({"error": "Invalid transaction data"}), 400

        logging.info(f"✅ معاملة مستلمة: {transaction_id} | المرسل: {sender_address} | المستلم: {recipient_address} | المبلغ: {amount}")

        # ✅ التحقق من أن الدفع ناجح
        if status.lower() != "completed":
            logging.warning(f"⚠️ لم يتم تأكيد المعاملة بعد، الحالة: {status}")
            return jsonify({"message": "Transaction not completed yet"}), 202

        logging.info(f"✅ تم تأكيد الدفع! المعاملة: {transaction_id}")

        # ✅ إرسال طلب إلى `/api/subscribe` لتحديث الاشتراك
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {WEBHOOK_SECRET}",
                "Content-Type": "application/json"
            }
            subscription_payload = {
                "telegram_id": telegram_id,  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
                "subscription_type_id": subscription_type_id,  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
                "payment_id": transaction_id, # نستخدم transaction_id كـ payment_id مؤقتًا
                "username": username,  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
                "full_name": full_name,  # ⚠️ قيمة وهمية - سيتم استبدالها لاحقًا بطريقة آمنة
                "webhook_sender_address": sender_address, # تمرير بيانات Webhook
                "webhook_recipient_address": recipient_address, # تمرير بيانات Webhook
                "webhook_amount": amount, # تمرير بيانات Webhook
                "webhook_status": status # تمرير بيانات Webhook
            }

            logging.info(f"📡 إرسال طلب تجديد الاشتراك إلى /api/subscribe (مباشر من Webhook): {json.dumps(subscription_payload, indent=2)}")

            async with session.post(SUBSCRIBE_API_URL, json=subscription_payload, headers=headers) as response:
                subscribe_response = await response.json()
                if response.status == 200:
                    logging.info(f"✅ تم تحديث الاشتراك بنجاح! الاستجابة: {subscribe_response}")
                    return jsonify({"message": "Subscription updated successfully"}), 200
                else:
                    logging.error(f"❌ فشل تحديث الاشتراك! الحالة: {response.status}, التفاصيل: {subscribe_response}")
                    return jsonify({"error": "Failed to update subscription"}), response.status

    except Exception as e:
        logging.error(f"❌ خطأ في Webhook (تصميم مثالي مبسط): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500