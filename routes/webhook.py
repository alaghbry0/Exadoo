# webhook.py (مع التحسينات)
import logging
import os
import aiohttp
import time
from quart import Blueprint, request, jsonify, current_app
import json
from database.db_queries import update_payment_with_txhash, fetch_pending_payment_by_wallet

webhook_bp = Blueprint("webhook", __name__)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
TONAPI_WEBHOOK_TOKEN = os.getenv("TONAPI_WEBHOOK_TOKEN")
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL", "http://localhost:5000/api/subscribe")

# مثال بسيط لتطبيق rate limiting (يمكن تطويره لاحقًا)
RATE_LIMIT = {}
RATE_LIMIT_WINDOW = 60  # فترة زمنية بالثواني
MAX_REQUESTS_PER_WINDOW = 10

def is_rate_limited(ip):
    current_time = time.time()
    record = RATE_LIMIT.get(ip, [])
    # احتفظ فقط بالطلبات الحديثة ضمن النافذة الزمنية
    record = [timestamp for timestamp in record if current_time - timestamp < RATE_LIMIT_WINDOW]
    RATE_LIMIT[ip] = record
    if len(record) >= MAX_REQUESTS_PER_WINDOW:
        return True
    record.append(current_time)
    return False

def log_request_info():
    """تسجيل تفاصيل الطلب عند استلامه."""
    logging.info("📥 استلام طلب جديد في /api/webhook")
    logging.debug(f"Headers: {dict(request.headers)}")
    logging.debug(f"IP Address: {request.remote_addr}")

def validate_secret():
    """التحقق من صحة مفتاح TONAPI_WEBHOOK_TOKEN في ترويسة الطلب"""
    secret = request.headers.get("Authorization")
    expected_auth = f"Bearer {TONAPI_WEBHOOK_TOKEN}"
    if not secret or secret != expected_auth:
        logging.warning(f"❌ Unauthorized webhook request! Received: {secret}, Expected: {expected_auth}")
        return False
    return True

def extract_webhook_payload(data: dict) -> dict:
    """
    دالة مساعدة لاستخراج البيانات الأساسية من payload للحدثين:
    "transaction_received" و "account_tx".
    كما يتم تحويل tx_hash إلى حروف صغيرة لتوحيد الصيغة.
    """
    payload = {}
    event_type = data.get("event_type")
    payload["event_type"] = event_type

    # استخراج tx_hash بشكل موحّد (تحويله إلى lowercase إن وجد)
    tx_hash = (data.get("tx_hash") or data.get("Tx_hash"))
    if tx_hash:
        payload["transaction_id"] = tx_hash.lower()
    else:
        payload["transaction_id"] = None

    if event_type == "transaction_received":
        # بيانات الحدث من "transaction_received"
        sender_address = data.get("data", {}).get("sender", {}).get("address")
        recipient_address = data.get("data", {}).get("recipient", {}).get("address")
        amount = data.get("data", {}).get("amount", 0)
        status = data.get("data", {}).get("status")
        payload.update({
            "sender_address": sender_address,
            "recipient_address": recipient_address,
            "amount": amount,
            "status": status,
            # هنا نستخدم عنوان المرسل للمطابقة في قاعدة البيانات
            "user_wallet_address": sender_address,
            "lt": None,
            "account_id": None
        })
    elif event_type == "account_tx":
        # بيانات الحدث من "account_tx"
        account_id = data.get("account_id")
        lt = data.get("lt")
        # محاولة استخراج عنوان المرسل من in_msg (إن وجد)
        sender_address = data.get("in_msg", {}).get("message", {}).get("info", {}).get("src", {}).get("address")
        # في حال عدم توفر sender_address، يمكن استخدام account_id كبديل
        sender_address = sender_address or account_id
        payload.update({
            "account_id": account_id,
            "lt": lt,
            "sender_address": sender_address,
            "user_wallet_address": sender_address,  # استخدام عنوان المرسل للمطابقة
            "recipient_address": None,
            "amount": None,
            "status": None
        })
    else:
        logging.error(f"❌ نوع الحدث غير معروف: {event_type}")

    return payload

@webhook_bp.route("/api/webhook", methods=["POST"])
async def webhook():
    """
    نقطة API لاستقبال إشعارات الدفع من TonAPI.
    """
    try:
        log_request_info()

        # تطبيق rate limiting بناءً على عنوان الـ IP
        ip = request.remote_addr
        if is_rate_limited(ip):
            logging.warning(f"❌ تم تجاوز حد الطلبات من العنوان: {ip}")
            return jsonify({"error": "Too many requests"}), 429

        # التحقق من صحة المفتاح
        if not validate_secret():
            return jsonify({"error": "Unauthorized request"}), 403

        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة: {json.dumps(data, indent=2)}")

        event_type = data.get("event_type")
        if event_type not in ["transaction_received", "account_tx"]:
            logging.info(f"⚠️ تجاهل حدث غير متعلق بالدفع: {event_type}")
            return jsonify({"message": "Event ignored"}), 200

        # استخراج البيانات باستخدام الدالة المساعدة
        payload = extract_webhook_payload(data)
        logging.debug(f"🔍 Debug - Full extracted payload: {json.dumps(payload, indent=2)}")

        # التحقق من صحة البيانات الأساسية
        if not payload.get("transaction_id"):
            logging.error("❌ tx_hash غير موجود في البيانات!")
            return jsonify({"error": "Missing transaction id"}), 400

        if event_type == "transaction_received":
            if not all([payload.get("sender_address"), payload.get("recipient_address"), payload.get("amount"), payload.get("status")]):
                logging.error("❌ بيانات transaction_received غير مكتملة!")
                return jsonify({"error": "Invalid transaction data"}), 400
            if payload.get("status").lower() != "completed":
                logging.warning(f"⚠️ لم يتم تأكيد المعاملة بعد، الحالة: {payload.get('status')}")
                return jsonify({"message": "Transaction not completed yet"}), 202

        elif event_type == "account_tx":
            if not all([payload.get("account_id"), payload.get("lt")]):
                logging.error("❌ بيانات account_tx غير مكتملة!")
                return jsonify({"error": "Invalid account transaction data"}), 400

        logging.info(
            f"✅ معاملة مستلمة: {payload.get('transaction_id')} | "
            f"المُرسل (user_wallet_address): {payload.get('user_wallet_address')} | "
            f"المستلم: {payload.get('recipient_address')} | "
            f"المبلغ: {payload.get('amount')}"
        )

        if not payload.get("user_wallet_address"):
            logging.error("❌ لم يتم العثور على عنوان المحفظة من Webhook")
            return jsonify({"error": "Missing wallet address from webhook data"}), 400

        # البحث عن سجل الدفع المعلق باستخدام عنوان المرسل (user_wallet_address)
        async with current_app.db_pool.acquire() as conn:
            payment_record = await fetch_pending_payment_by_wallet(conn, payload.get("user_wallet_address"))
        if not payment_record:
            logging.error(f"❌ لم يتم العثور على سجل دفع معلق لعنوان المحفظة: {payload.get('user_wallet_address')}")
            return jsonify({"error": "Pending payment record not found for this wallet address"}), 404

        # تحديث سجل الدفع في قاعدة البيانات باستخدام payment_id لتسجيل tx_hash
        async with current_app.db_pool.acquire() as conn:
            updated_payment_record = await update_payment_with_txhash(
                conn,
                payment_record.get('payment_id'),
                payload.get("transaction_id")
            )
        if not updated_payment_record:
            logging.error("❌ فشل تحديث سجل الدفع")
            return jsonify({"error": "Failed to update payment record"}), 500

        # استخدام البيانات الفعلية من سجل الدفع المُحدث لتجهيز بيانات الاشتراك
        telegram_id = payment_record.get("telegram_id")
        subscription_type_id = payment_record.get("subscription_type_id")
        username = payment_record.get("username")
        full_name = payment_record.get("full_name")

        subscription_payload = {
            "telegram_id": telegram_id,
            "subscription_type_id": subscription_type_id,
            "payment_id": payload.get("transaction_id"),  # استخدام tx_hash النهائي
            "username": username,
            "full_name": full_name,
            # في حالة account_tx يمكن إرسال account_id و lt، وإلا تُترك كـ None
            "webhook_account_id": payload.get("account_id"),
            "webhook_sender_address": payload.get("sender_address"),
            "webhook_recipient_address": payload.get("recipient_address"),
            "webhook_amount": payload.get("amount"),
            "webhook_status": payload.get("status"),
            "webhook_lt": payload.get("lt")
        }

        logging.info(f"📡 إرسال طلب تجديد الاشتراك إلى /api/subscribe: {json.dumps(subscription_payload, indent=2)}")
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {WEBHOOK_SECRET}",
                "Content-Type": "application/json"
            }
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
