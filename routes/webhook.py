import logging
import os
import aiohttp
from quart import Blueprint, request, jsonify, current_app
import json  # استيراد مكتبة json

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
    if not secret or secret != f"Bearer {TONAPI_WEBHOOK_TOKEN}":
        logging.warning("❌ Unauthorized webhook request: Invalid or missing TONAPI_WEBHOOK_TOKEN")
        return False
    return True


@webhook_bp.route("/api/webhook", methods=["POST"])
async def webhook():
    """
    نقطة API لاستقبال إشعارات الدفع من TonAPI (محسّنة).
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

        if event_type == "transaction_received":
            logging.info(f"✅ معالجة حدث transaction_received")
            transaction = data.get("data", {})
            transaction_id = transaction.get("tx_hash")
            sender_address = transaction.get("sender", {}).get("address")
            recipient_address = transaction.get("recipient", {}).get("address")
            amount = transaction.get("amount", 0)
            status = transaction.get("status")

            # ✅ التحقق من البيانات المطلوبة لـ transaction_received
            if not all([transaction_id, sender_address, recipient_address, amount, status]):
                logging.error("❌ بيانات الدفع غير مكتملة لـ transaction_received!")
                return jsonify({"error": "Invalid transaction data"}), 400

            logging.info(
                f"✅ معاملة مستلمة (transaction_received): {transaction_id} | المرسل: {sender_address} | المستلم: {recipient_address} | المبلغ: {amount}")

        elif event_type == "account_tx":
            logging.info(f"✅ معالجة حدث account_tx")
            account_tx_data = data.get("data", {}) # تغيير اسم المتغير لتوضيح نوع البيانات
            transaction_id = account_tx_data.get("tx_hash")
            account_address = data.get("account_id") # استخراج account_id من المستوى الأعلى

            sender_address = account_address # استخدام account_id كعنوان مرسل ومستقبل مؤقت
            recipient_address = account_address # استخدام account_id كعنوان مرسل ومستقبل مؤقت
            amount = None # لا يوجد مبلغ في account_tx
            status = "unknown" # الحالة غير معروفة في account_tx

            # ✅ التحقق من البيانات المطلوبة لـ account_tx
            if not all([transaction_id, account_address]):
                logging.error("❌ بيانات الدفع غير مكتملة لـ account_tx!")
                return jsonify({"error": "Invalid transaction data for account_tx"}), 400

            logging.info(
                f"✅ معاملة مستلمة (account_tx): {transaction_id} | Account ID: {account_address}") # تسجيل Account ID بدلاً من المرسل والمستقبل

        else:
            logging.info(f"⚠️ تجاهل حدث غير متعلق بالدفع: {event_type}")
            return jsonify({"message": "Event ignored"}), 200


        # ✅ التحقق من أن الدفع ناجح (فقط لـ transaction_received)
        if event_type == "transaction_received" and status.lower() != "completed":
            logging.warning(f"⚠️ لم يتم تأكيد المعاملة بعد، الحالة: {status}")
            return jsonify({"message": "Transaction not completed yet"}), 202

        if event_type == "transaction_received":
            logging.info(f"✅ تم تأكيد الدفع! المعاملة: {transaction_id}")
        elif event_type == "account_tx":
            logging.info(f"✅ تم استلام اشعار account_tx للمعاملة: {transaction_id}")


        # ✅ بيانات المستخدم والاشتراك (يجب تحسينها لاحقًا)
        telegram_id = 7382197778  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        subscription_type_id = 1  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        username = "test_user"  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا
        full_name = "Test User"  # ⚠️ قيمة افتراضية - ستُستبدل لاحقًا

        # ✅ إرسال طلب إلى `/api/subscribe` لتحديث الاشتراك
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
                "webhook_sender_address": sender_address,
                "webhook_recipient_address": recipient_address,
                "webhook_amount": amount,
                "webhook_status": status
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