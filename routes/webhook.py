# webhook.py (modified - attempt to extract sender address and enhanced logging)
import logging
import os
import aiohttp
from quart import Blueprint, request, jsonify, current_app
import json
from database.db_queries import update_payment_with_txhash, fetch_pending_payment_by_wallet

webhook_bp = Blueprint("webhook", __name__)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
TONAPI_WEBHOOK_TOKEN = os.getenv("TONAPI_WEBHOOK_TOKEN")
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL", "http://localhost:5000/api/subscribe")

# قاموس لتتبع محاولات إعادة المحاولة (مؤقت - سيتم استبداله بآلية أكثر قوة لاحقًا إذا لزم الأمر)
retry_attempts = {}
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 600 # 10 دقائق

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
    """
    try:
        # تسجيل معلومات الطلب
        log_request_info()

        # التحقق من صحة المفتاح
        if not validate_secret():
            return jsonify({"error": "Unauthorized request"}), 403

        # استقبال بيانات المعاملة
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة: {json.dumps(data, indent=2)}")

        # استخراج نوع الحدث
        event_type = data.get("event_type")
        if event_type not in ["transaction_received", "account_tx"]:
            logging.info(f"⚠️ تجاهل حدث غير متعلق بالدفع: {event_type}")
            return jsonify({"message": "Event ignored"}), 200

        # استخراج بيانات المعاملة الأساسية
        transaction_id = data.get("tx_hash") or data.get("Tx_hash")   # محاولة الحصول على tx_hash بحالات مختلفة

        if event_type == "account_tx":
            account_id = data.get("account_id")
            lt = data.get("lt")
            # **محاولة استخراج عنوان المرسل من بيانات account_tx - تخمين مبدئي**
            sender_address_webhook = data.get("in_msg", {}).get("message", {}).get("info", {}).get("src", {}).get("address")
            user_wallet_address_webhook = sender_address_webhook  # استخدام عنوان المرسل للبحث
            # القيم التالية غير متوفرة في account_tx
            sender_address = sender_address_webhook  # تعيين sender_address للمعلومات المسجلة
            recipient_address = None  # غير متوفر في account_tx بشكل مباشر
            amount = None
            status = None

            # **تسجيل حمولة البيانات الكاملة لحدث account_tx للتصحيح**
            logging.info(f"🔍 Debug - Full account_tx payload: {json.dumps(data, indent=2)}")


        else:   # event_type == "transaction_received"
            account_id = None
            lt = None
            sender_address = data.get("data", {}).get("sender", {}).get("address")
            recipient_address = data.get("data", {}).get("recipient", {}).get("address")
            amount = data.get("data", {}).get("amount", 0)
            status = data.get("data", {}).get("status")
            user_wallet_address_webhook = sender_address

        logging.info(f"✅ معاملة مستلمة: {transaction_id} | الحساب: {user_wallet_address_webhook} | المرسل: {sender_address} | المستلم: {recipient_address} | المبلغ: {amount}")

        # التحقق من وجود عنوان المحفظة
        if not user_wallet_address_webhook:
            logging.error("❌ لم يتم العثور على عنوان المحفظة من Webhook")
            return jsonify({"error": "Missing wallet address from webhook data"}), 400

        # البحث عن سجل الدفع المعلق باستخدام user_wallet_address_webhook
        async with current_app.db_pool.acquire() as conn:
            payment_record = await fetch_pending_payment_by_wallet(conn, user_wallet_address_webhook)
        if not payment_record:
            payment_id_key = transaction_id or user_wallet_address_webhook # استخدام tx_hash أو عنوان المحفظة كمفتاح لإعادة المحاولة

            attempt = retry_attempts.get(payment_id_key, 0)
            if attempt < MAX_RETRIES:
                retry_attempts[payment_id_key] = attempt + 1
                logging.warning(f"⚠️ لم يتم العثور على سجل دفع معلق لعنوان المحفظة: {user_wallet_address_webhook}. محاولة إعادة المحاولة رقم: {attempt + 1} بعد {RETRY_DELAY_SECONDS} ثانية...")
                return jsonify({"error": "Pending payment record not found, retrying"}), 500 # إرجاع 500 لمحاولة إعادة الإرسال
            else:
                logging.error(f"❌ تم الوصول إلى الحد الأقصى لعدد محاولات إعادة المحاولة ({MAX_RETRIES}) لسجل الدفع المعلق لعنوان المحفظة: {user_wallet_address_webhook}")
                return jsonify({"error": "Pending payment record not found after multiple retries"}), 404


        # تحديث سجل الدفع في قاعدة البيانات باستخدام payment_id لتسجيل tx_hash
        async with current_app.db_pool.acquire() as conn:
            updated_payment_record = await update_payment_with_txhash(conn, payment_record.get('payment_id'), transaction_id)
        if not updated_payment_record:
            return jsonify({"error": "Failed to update payment record"}), 500

        # استخدام البيانات الفعلية من سجل الدفع المُحدث
        telegram_id = payment_record.get("telegram_id")
        subscription_type_id = payment_record.get("subscription_type_id")
        username = payment_record.get("username")
        full_name = payment_record.get("full_name")

        # تجهيز بيانات الاشتراك باستخدام المعلومات الفعلية
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {WEBHOOK_SECRET}",
                "Content-Type": "application/json"
            }
            subscription_payload = {
                "telegram_id": telegram_id,
                "subscription_type_id": subscription_type_id,
                "payment_id": transaction_id,   # استخدام tx_hash النهائي
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