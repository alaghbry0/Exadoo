import logging
import json
import os
import asyncio
import asyncpg
from quart import Blueprint, request, jsonify, current_app


# 🔹 إنشاء Blueprint لمعاملات الدفع
payments_bp = Blueprint("payments", __name__)

# 🔹 عنوان API الخاص بتحديث الاشتراك
webhook_url = os.getenv("SUBSCRIBE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ✅ تحميل `WEBHOOK_SECRET`

@payments_bp.route("/webhook", methods=["POST"])
async def telegram_webhook():
    """🔄 استلام الدفع وإرساله إلى `/api/subscribe`"""

    # ✅ التحقق من `WEBHOOK_SECRET`
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != WEBHOOK_SECRET:
        logging.error("❌ Webhook request غير موثوق! تم رفضه.")
        return jsonify({"error": "Unauthorized request"}), 403

    try:
        data = await request.get_json()
        logging.info(f"📥 Webhook received: {json.dumps(data, indent=2)}")

        # ✅ التأكد من أن التحديث يحتوي على "successful_payment"
        payment = data.get("message", {}).get("successful_payment", None)
        if not payment:
            logging.warning("⚠️ Webhook received a non-payment update. Ignoring it.")
            return jsonify({"message": "Ignored non-payment update"}), 200

        # ✅ استخراج بيانات الدفع
        payment = data.get("message", {}).get("successful_payment", {})

        try:
            payload = json.loads(payment.get("invoice_payload", "{}"))  # ✅ تأكد من استخراج `invoice_payload` الصحيح
        except json.JSONDecodeError as e:
            logging.error(f"❌ فشل في فك تشفير `invoice_payload`: {e}")
            return jsonify({"error": "Invalid invoice payload"}), 400

        telegram_id = payload.get("userId")
        subscription_type_id = payload.get("planId")
        payment_id = payment.get("telegram_payment_charge_id")

        # ✅ التحقق من صحة البيانات المستلمة
        if not isinstance(telegram_id, int) or not isinstance(subscription_type_id, int) or not isinstance(payment_id, str):
            logging.error(
                f"❌ Invalid data format: telegram_id={telegram_id}, subscription_type_id={subscription_type_id}, payment_id={payment_id}")
            return jsonify({"error": "Invalid payment data format"}), 400

        # ✅ إعداد البيانات الصحيحة قبل الإرسال
        payload = {
            "telegram_id": telegram_id,
            "subscription_type_id": subscription_type_id,
            "payment_id": payment_id
        }

        # ✅ إرسال الطلب إلى `/api/subscribe`
        success = await send_to_subscribe_api(payload)
        if success:
            return jsonify({"message": "Subscription updated successfully"}), 200
        else:
            return jsonify({"error": "Subscription update failed"}), 500

    except json.JSONDecodeError as e:
        logging.error(f"❌ JSON Decode Error: {e}")
        return jsonify({"error": "Invalid JSON format"}), 400
    except asyncpg.PostgresError as e:
        logging.error(f"❌ Database Error: {e}")
        return jsonify({"error": "Database error"}), 500
    except Exception as e:
        logging.exception(f"❌ خطأ غير متوقع: {e}")
        return jsonify({"error": "Internal server error"}), 500


async def send_to_subscribe_api(payload, max_retries=3):
    """🔁 إرسال بيانات الدفع إلى `/api/subscribe` مع `Retry` في حالة الفشل"""

    session = getattr(current_app, "aiohttp_session", None)
    if not session or session.closed:
        logging.critical("❌ جلسة aiohttp غير صالحة!")
        return False

    headers = {
        "Authorization": f"Bearer {WEBHOOK_SECRET}"
    }

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"🚀 إرسال طلب الاشتراك إلى {SUBSCRIBE_URL} - المحاولة {attempt}/{max_retries}")
            async with session.post(SUBSCRIBE_URL, json=payload, headers=headers) as resp:
                response_text = await resp.text()
                logging.info(f"🔹 استجابة API: {resp.status} - {response_text}")

                if resp.status == 200:
                    logging.info("✅ تم تجديد الاشتراك بنجاح!")
                    return True
                else:
                    logging.error(f"❌ فشل تجديد الاشتراك، المحاولة {attempt}/{max_retries}: {response_text}")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # ⏳ تأخير تصاعدي بين المحاولات

    logging.critical("🚨 جميع محاولات تحديث الاشتراك فشلت!")
    return False
