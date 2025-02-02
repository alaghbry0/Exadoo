import logging
import aiohttp
from quart import Blueprint, request, jsonify, current_app
from routes.subscriptions import subscribe  # ✅ استيراد وظيفة `subscribe` لإضافة الاشتراك بعد نجاح الدفع

payments_bp = Blueprint("payments", __name__)

TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # ضع هنا التوكن الخاص بالبوت
TELEGRAM_API_URL = "https://api.telegram.org/bot{}/sendInvoice".format(TELEGRAM_BOT_TOKEN)


@payments_bp.route("/api/payments/telegram-stars", methods=["POST"])
async def process_telegram_stars_payment():
    """
    ✅ نقطة API لاستقبال طلب الدفع بـ Telegram Stars ومعالجته
    """
    try:
        data = await request.get_json()
        telegram_id = data.get("telegram_id")
        subscription_id = data.get("subscription_id")
        amount = data.get("amount")

        logging.info(
            f"📥 Received Telegram Stars payment request: telegram_id={telegram_id}, subscription_id={subscription_id}, amount={amount}")

        if not telegram_id or not subscription_id or not amount:
            return jsonify({"error": "جميع الحقول مطلوبة (telegram_id, subscription_id, amount)"}), 400

        async with aiohttp.ClientSession() as session:
            payload = {
                "chat_id": telegram_id,
                "title": "دفع الاشتراك",
                "description": f"دفع اشتراك بقيمة {amount} Telegram Stars",
                "payload": f"subscription_{subscription_id}_{telegram_id}",
                "provider_token": "YOUR_TELEGRAM_PROVIDER_TOKEN",  # استبدله بتوكن بوابة الدفع الخاصة بتليجرام
                "currency": "USD",
                "prices": [{"label": "اشتراك", "amount": int(amount * 100)}]  # تحويل إلى سنتات
            }

            async with session.post(TELEGRAM_API_URL, json=payload) as response:
                telegram_response = await response.json()

                if response.status != 200 or not telegram_response.get("ok"):
                    logging.error(f"❌ Telegram Stars API error: {telegram_response}")
                    return jsonify({"error": "فشل معالجة الدفع عبر Telegram Stars"}), 500

        # ✅ الدفع ناجح، نقوم الآن بمنح الاشتراك
        response = await subscribe()  # ✅ استدعاء `/api/subscribe` لإضافة الاشتراك
        return jsonify(
            {"message": "تمت معالجة الدفع بنجاح وتم تفعيل الاشتراك", "subscription_response": response.json}), 200

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة الدفع بـ Telegram Stars: {str(e)}", exc_info=True)
        return jsonify({"error": "حدث خطأ أثناء معالجة الدفع"}), 500