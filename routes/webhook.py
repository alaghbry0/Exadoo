import logging
import os
import aiohttp
from quart import Blueprint, request, jsonify
import json

webhook_bp = Blueprint("webhook", __name__)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
TONAPI_WEBHOOK_TOKEN = os.getenv("TONAPI_WEBHOOK_TOKEN")
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL", "http://localhost:5000/api/subscribe")

def log_request_info():
    """تسجيل تفاصيل الطلب عند استلامه."""
    logging.info("\n📥 استلام طلب جديد في /api/webhook")
    logging.info(f"🔹 Headers: {dict(request.headers)}")
    logging.info(f"🔹 IP Address: {request.remote_addr}")

def validate_secret():
    """التحقق من صحة مفتاح WEBHOOK_SECRET"""
    secret = request.headers.get("Authorization")
    expected_auth = f"Bearer {TONAPI_WEBHOOK_TOKEN}"
    if not secret or secret != expected_auth:
        logging.warning(f"❌ Unauthorized webhook request! Received: {secret}, Expected: {expected_auth}")
        return False
    return True

@webhook_bp.route("/api/webhook", methods=["POST"])
async def webhook():
    """
    نقطة API لاستقبال إشعارات الدفع من TonAPI.
    Webhook هو المصدر الموثوق لتفعيل الاشتراك.
    """
    try:
        # ✅ تسجيل معلومات الطلب
        log_request_info()

        # ✅ التحقق من WEBHOOK_SECRET قبل معالجة البيانات
        if not validate_secret():
            return jsonify({"error": "Unauthorized request"}), 403

        # ✅ استقبال البيانات
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة: {json.dumps(data, indent=2)}")

        # ✅ استخراج نوع الحدث بشكل صحيح
        event_type = data.get("event_type")

        # ✅ دعم `transaction_received` و `account_tx`
        if event_type not in ["transaction_received", "account_tx"]:
            logging.info(f"⚠️ تجاهل حدث غير متعلق بالدفع: {event_type}")
            return jsonify({"message": "Event ignored"}), 200

        # ✅ بيانات الدفع المشتركة
        transaction_id = data.get("tx_hash")
        account_id = data.get("account_id") if event_type == "account_tx" else None
        lt = data.get("lt") if event_type == "account_tx" else None
        sender_address = data.get("data", {}).get("sender", {}).get("address") if event_type == "transaction_received" else None
        recipient_address = data.get("data", {}).get("recipient", {}).get("address") if event_type == "transaction_received" else None
        amount = data.get("data", {}).get("amount", 0) if event_type == "transaction_received" else None
        status = data.get("data", {}).get("status") if event_type == "transaction_received" else None

        # ✅ التحقق من البيانات المطلوبة بناءً على نوع الحدث
        if event_type == "transaction_received":
            if not all([transaction_id, sender_address, recipient_address, amount, status]):
                logging.error("❌ بيانات `transaction_received` غير مكتملة!")
                return jsonify({"error": "Invalid transaction data"}), 400
            if status.lower() != "completed":
                logging.warning(f"⚠️ لم يتم تأكيد المعاملة بعد، الحالة: {status}")
                return jsonify({"message": "Transaction not completed yet"}), 202
        elif event_type == "account_tx":
            if not all([transaction_id, account_id, lt]):
                logging.error("❌ بيانات `account_tx` غير مكتملة!")
                return jsonify({"error": "Invalid account transaction data"}), 400

        logging.info(f"✅ معاملة مستلمة: {transaction_id} | الحساب: {account_id if event_type == 'account_tx' else sender_address} | المستلم: {recipient_address} | المبلغ: {amount}")

        # ✅ بيانات المستخدم والاشتراك (يجب تحسينها لاحقًا)
        telegram_id = 7382197778  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        subscription_type_id = 1  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        username = "test_user"  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        full_name = "Test User"  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا

        # ✅ تجهيز بيانات الاشتراك وإرسالها إلى `/api/subscribe`
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {WEBHOOK_SECRET}",
                "Content-Type": "application/json"
            }
            subscription_payload = {
                "telegram_id": telegram_id,
                "subscription_type_id": subscription_type_id,
                "payment_id": transaction_id,
                "username": username,
                "full_name": full_name,
                "webhook_account_id": account_id if event_type == "account_tx" else None,
                "webhook_sender_address": sender_address,
                "webhook_recipient_address": recipient_address,
                "webhook_amount": amount,
                "webhook_status": status,
                "webhook_lt": lt if event_type == "account_tx" else None
            }

            logging.info(f"📡 إرسال طلب تجديد الاشتراك إلى /api/subscribe: {json.dumps(subscription_payload, indent=2)}")

            async with session.post(SUBSCRIBE_API_URL, json=subscription_payload, headers=headers) as response:
                subscribe_response = await response.json()
                if response.status == 200:
                    logging.info(f"✅ تم تحديث الاشتراك بنجاح! الاستجابة: {subscribe_response}")
                    return jsonify({"message": "Subscription updated successfully"}), 200
                else:
                    logging.error(f"❌ فشل تحديث الاشتراك! الحالة: {response.status}, التفاصيل: {subscribe_response}")
                    return jsonify({"error": "Failed to update subscription"}), response.status

    except Exception as e:
        logging.error(f"❌ خطأ في Webhook: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500