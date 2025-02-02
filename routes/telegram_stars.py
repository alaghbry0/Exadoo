
import logging
import aiohttp
from quart import Blueprint, request, jsonify, current_app
from routes.subscriptions import subscribe
from config import TELEGRAM_BOT_TOKEN

payments_bp = Blueprint("payments", __name__)

TELEGRAM_API_URL = f"https://api.telegram.org/bot{telegram_bot}/createInvoiceLink"


async def validate_payment(invoice_id: str):
    """التحقق من حالة الدفع عبر API"""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{telegram_bot}/getPaymentInfo"
        async with session.post(url, json={"invoice_id": invoice_id}) as resp:
            return await resp.json()


@payments_bp.route("/api/payments/create-invoice", methods=["POST"])
async def create_invoice():
    try:
        data = await request.get_json()
        required_fields = ['telegram_id', 'subscription_id', 'amount']

        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields"}), 400

        # إنشاء payload للفاتورة
        payload = {
            "chat_id": data['telegram_id'],
            "title": "تجديد الاشتراك",
            "description": f"اشتراك بقيمة {data['amount']} نجوم",
            "payload": f"sub_{data['subscription_id']}_{data['telegram_id']}",
            "provider_token": "YOUR_PAYMENT_PROVIDER_TOKEN",
            "currency": "USD",
            "prices": [{"label": "الاشتراك", "amount": int(data['amount'] * 100)}],
            "need_name": False,
            "need_phone_number": False,
            "need_email": False,
            "send_phone_number_to_provider": False,
            "send_email_to_provider": False,
            "is_flexible": False
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(TELEGRAM_API_URL, json=payload) as resp:
                response_data = await resp.json()

                if not response_data.get('ok'):
                    logging.error(f"Invoice creation failed: {response_data}")
                    return jsonify({"error": "فشل في إنشاء الفاتورة"}), 500

                return jsonify({
                    "invoice_link": response_data['result'],
                    "subscription_id": data['subscription_id'],
                    "telegram_id": data['telegram_id']
                })

    except Exception as e:
        logging.error(f"Payment error: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@payments_bp.route("/api/payments/confirm", methods=["POST"])
async def confirm_payment():
    try:
        data = await request.get_json()
        payment_status = await validate_payment(data['invoice_id'])

        if payment_status.get('status') != 'paid':
            return jsonify({"error": "Payment not completed"}), 400

        # تفعيل الاشتراك
        await subscribe(
            telegram_id=data['telegram_id'],
            subscription_id=data['subscription_id']
        )

        return jsonify({"success": True})

    except Exception as e:
        logging.error(f"Payment confirmation error: {str(e)}", exc_info=True)
        return jsonify({"error": "Payment verification failed"}), 500