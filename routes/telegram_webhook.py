import logging
import json
import os
import asyncio
import asyncpg
import ipaddress
from quart import Blueprint, request, jsonify, current_app
from database.db_queries import record_payment

# 🔹 إنشاء Blueprint لمعاملات الدفع
payments_bp = Blueprint("payments", __name__)

# 🔹 تحميل متغيرات البيئة
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not SUBSCRIBE_URL:
    raise ValueError("❌ `SUBSCRIBE_URL` غير معرف! تأكد من ضبطه في المتغيرات البيئية.")

# 🔹 قائمة عناوين IP الرسمية الخاصة بتليجرام (يجب تحديثها عند الحاجة)
TELEGRAM_IP_RANGES = [
    "149.154.160.0/20", "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22",
    "91.108.16.0/22", "91.108.20.0/22", "91.108.56.0/22", "149.154.164.0/22",
    "149.154.168.0/22", "149.154.172.0/22", "91.105.192.0/23"
]

def is_request_from_telegram(ip_address):
    """🔹 التحقق مما إذا كان الطلب قادمًا من خوادم تليجرام"""
    try:
        ip = ipaddress.ip_address(ip_address)
        return any(ip in ipaddress.ip_network(cidr) for cidr in TELEGRAM_IP_RANGES)
    except ValueError:
        return False

@payments_bp.route("/webhook", methods=["POST"])
async def telegram_webhook():
    """🔄 استقبال الدفع وتحديث الاشتراك"""
    try:
        # ✅ الحصول على عنوان الـ IP للطلب
        request_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        logging.info(f"📥 Webhook request received from IP: {request_ip}")

        # ✅ التأكد من أن الطلب قادم من خوادم تليجرام
        if not request_ip or not is_request_from_telegram(request_ip):
            logging.error(f"❌ Webhook request مرفوض! IP غير موثوق: {request_ip}")
            return jsonify({"error": "Unauthorized request"}), 403

        # ✅ الحصول على البيانات المستلمة
        data = await request.get_json()
        logging.info(f"📥 Webhook received: {json.dumps(data, indent=2)}")

        # ✅ التأكد من أن التحديث يحتوي على "successful_payment"
        payment = data.get("message", {}).get("successful_payment", None)

        if not payment:
            logging.warning("⚠️ Webhook لم يستلم `successful_payment`. Ignoring.")
            return jsonify({"message": "Ignored non-payment update"}), 200

        # ✅ معالجة بيانات الدفع
        try:
            payload = json.loads(payment.get("invoice_payload", "{}"))
        except json.JSONDecodeError as e:
            logging.error(f"❌ فشل في فك تشفير `invoice_payload`: {e}")
            return jsonify({"error": "Invalid invoice payload"}), 400

        telegram_id = payload.get("userId")
        subscription_type_id = payload.get("planId")
        payment_id = payment.get("telegram_payment_charge_id")
        amount = payment.get("total_amount", 0) // 100  # تحويل النجوم إلى الدولار

        if not all([telegram_id, subscription_type_id, payment_id]):
            logging.error(f"❌ بيانات ناقصة في `successful_payment`: {data}")
            return jsonify({"error": "Invalid payment data"}), 400

        logging.info(f"✅ استلام دفعة جديدة من {telegram_id} للخطة {subscription_type_id}, مبلغ: {amount}")

        # ✅ تسجيل الدفع في قاعدة البيانات
        db_pool = getattr(current_app, "db_pool", None)
        if not db_pool:
            logging.error("❌ قاعدة البيانات غير متاحة!")
            return jsonify({"error": "Database connection error"}), 500

        async with db_pool.acquire() as conn:
            existing_payment = await conn.fetchrow("SELECT * FROM payments WHERE payment_id = $1", payment_id)
            if existing_payment:
                logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                return jsonify({"message": "Payment already recorded"}), 200

            await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount,
                                 subscription_type_id=subscription_type_id)

        # ✅ إرسال الطلب إلى `/api/subscribe`
        payload = {
            "telegram_id": telegram_id,
            "subscription_type_id": subscription_type_id,
            "payment_id": payment_id
        }
        success = await send_to_subscribe_api(payload)

        if success:
            return jsonify({"message": "Subscription updated successfully"}), 200
        else:
            return jsonify({"error": "Subscription update failed"}), 500

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة Webhook: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


async def send_to_subscribe_api(payload, max_retries=3):
    """🔁 إرسال بيانات الدفع إلى `/api/subscribe` مع `Retry` في حالة الفشل"""
    session = getattr(current_app, "aiohttp_session", None)
    if not session or session.closed:
        logging.critical("❌ جلسة `aiohttp` غير صالحة!")
        return False

    headers = {
        "Authorization": f"Bearer {WEBHOOK_SECRET}",
        "Content-Type": "application/json"
    }

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"🚀 إرسال طلب الاشتراك إلى {SUBSCRIBE_URL} - المحاولة {attempt}/{max_retries}")
            async with session.post(SUBSCRIBE_URL, json=payload, headers=headers) as resp:
                response_text = await resp.text()
                logging.info(f"🔹 استجابة API: {resp.status} - {response_text}")

                if resp.status == 200:
                    logging.info("✅ تم تحديث الاشتراك بنجاح!")
                    return True
                else:
                    logging.error(f"❌ فشل تحديث الاشتراك، المحاولة {attempt}/{max_retries}: {response_text}")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # ⏳ تأخير متزايد بين المحاولات (2s, 4s, 8s)

    logging.critical("🚨 جميع محاولات تحديث الاشتراك فشلت!")
    return False
