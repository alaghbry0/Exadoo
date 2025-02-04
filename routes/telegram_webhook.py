import logging
import json
import os
import asyncio
import asyncpg
from dotenv import load_dotenv
from quart import Blueprint, request, jsonify, current_app
from aiogram.utils.web_app import check_webapp_signature
from database.db_queries import record_payment

load_dotenv()

# 🔹 إنشاء Blueprint لمعاملات الدفع
payments_bp = Blueprint("payments", __name__)

# 🔹 عنوان API الخاص بتحديث الاشتراك
SUBSCRIBE_URL = "https://exadoo.onrender.com/api/subscribe"


# 🔹 Webhook لمعالجة المدفوعات الواردة من تليجرام
@payments_bp.route("/webhook", methods=["POST"])
async def telegram_webhook():
    """نقطة استقبال مدفوعات تليجرام عبر Webhook"""

    # ✅ التحقق من WEBHOOK_SECRET
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != os.getenv("WEBHOOK_SECRET"):
        logging.error("❌ Webhook request غير موثوق!")
        return jsonify({"error": "Unauthorized request"}), 403

    try:
        data = await request.get_json()

        # ✅ التأكد من وجود بيانات الدفع
        payment = data.get("message", {}).get("successful_payment")
        if not payment:
            logging.warning("⚠️ تم استقبال Webhook بدون بيانات دفع")
            return jsonify({"error": "No payment data"}), 400

        # ✅ استخراج البيانات من الطلب
        payload = json.loads(payment["invoice_payload"])
        telegram_id = payload.get("userId")
        plan_id = payload.get("planId")
        payment_id = payment.get("telegram_payment_charge_id")
        amount = payment.get("total_amount", 0) // 100  # تحويل النجوم إلى الدولار

        logging.info(f"✅ استلام دفعة جديدة من {telegram_id} للخطة {plan_id}, مبلغ: {amount}")

        # ✅ التحقق من توفر اتصال بقاعدة البيانات
        db_pool = getattr(current_app, "db_pool", None)
        if not db_pool:
            logging.error("❌ قاعدة البيانات غير متاحة!")
            return jsonify({"error": "Database unavailable"}), 500

        async with db_pool.acquire() as conn:
            # ✅ التحقق من أن الدفع غير مسجل مسبقًا
            existing_payment = await conn.fetchrow("SELECT * FROM payments WHERE payment_id = $1", payment_id)
            if existing_payment:
                logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                return jsonify({"message": "Payment already processed"}), 200

            # ✅ تسجيل الدفع في قاعدة البيانات
            await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount, plan_id=plan_id)

        # ✅ إرسال طلب إلى API Next.js لتجديد الاشتراك
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        headers = {"Authorization": f"Bearer {webhook_secret}"}
        payload = {
            "telegram_id": telegram_id,
            "subscription_type_id": plan_id,
            "payment_id": payment_id
        }

        success = await send_subscription_request(payload, headers)
        if success:
            return jsonify({"message": "Subscription updated successfully"}), 200
        else:
            return jsonify({"error": "Subscription update failed"}), 500

    except Exception as e:
        logging.error(f"❌ خطأ في Webhook الدفع: {e}")
        return jsonify({"error": "Internal server error"}), 500


async def send_subscription_request(payload, headers, max_retries=3):
    """
    🔁 دالة لإرسال طلب تجديد الاشتراك مع `Retry` في حالة الفشل.
    ✅ يتم استخدام `current_app.aiohttp_session` بدلاً من إنشاء جلسة جديدة.
    """
    session = getattr(current_app, "aiohttp_session", None)
    if not session or session.closed:
        logging.critical("❌ جلسة aiohttp غير صالحة!")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            async with session.post(SUBSCRIBE_URL, json=payload, headers=headers) as resp:
                response_text = await resp.text()

                if resp.status == 200:
                    logging.info("✅ تم تجديد الاشتراك بنجاح!")
                    return True
                else:
                    logging.error(f"❌ فشل تجديد الاشتراك، المحاولة {attempt}/{max_retries}: {response_text}")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # ⏳ انتظار قبل إعادة المحاولة

    logging.critical("🚨 جميع محاولات تحديث الاشتراك فشلت!")
    return False
