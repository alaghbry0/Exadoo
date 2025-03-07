# server/sse_routes.py
from quart import Blueprint, request, Response, jsonify
import logging
import asyncio
from server.redis_manager import redis_manager
from database.db_queries import validate_payment_owner

sse_bp = Blueprint('sse', __name__)

async def event_generator(payment_token):
    pubsub = None  # تعريف مسبق للمتغير
    try:
        pubsub = redis_manager.redis.pubsub()
        await pubsub.subscribe(f'payment_{payment_token}')
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=30
            )
            if message:
                yield f"data: {message['data']}\n\n"
            await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"SSE error: {str(e)}")
    finally:
        if pubsub:  # التحقق من وجود اتصال قبل الإغلاق
            await pubsub.unsubscribe(f'payment_{payment_token}')
            logging.info(f"🏁 SSE connection closed for {payment_token}")

@sse_bp.route('/sse')
async def sse_stream():
    payment_token = request.args.get('payment_token')
    telegram_id = request.headers.get('X-Telegram-Id')

    # التحقق من وجود المعلمات
    if not payment_token or not telegram_id:
        logging.warning("❌ معلمات ناقصة في طلب SSE")
        return jsonify({"error": "المعلمات المطلوبة: payment_token و X-Telegram-Id"}), 400

    # التحقق من صحة Telegram ID
    try:
        telegram_id = int(telegram_id)
    except ValueError:
        logging.error(f"❌ تنسيق Telegram ID غير صالح: {telegram_id}")
        return jsonify({"error": "تنسيق Telegram ID غير صالح"}), 400

    # التحقق من ملكية الدفع باستخدام الاتصال بقاعدة البيانات
    # نفترض هنا أن الاتصال بقاعدة البيانات محفوظ في request.app.db
    if not await validate_payment_owner(request.app.db, payment_token, telegram_id):
        logging.warning(f"❌ وصول غير مصرح به لـ Telegram ID: {telegram_id}")
        return jsonify({"error": "غير مصرح به"}), 403

    return Response(
        event_generator(payment_token),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )
