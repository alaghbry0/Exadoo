# server/sse_routes.py
from quart import Blueprint, request, Response, jsonify, current_app
import logging
import asyncio
from server.redis_manager import redis_manager

sse_bp = Blueprint('sse', __name__)

async def event_generator(payment_token):
    pubsub = None
    try:
        logging.info(f"بدء الاشتراك في قناة Redis: payment_{payment_token}")
        pubsub = redis_manager.redis.pubsub()
        await pubsub.subscribe(f'payment_{payment_token}')
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=30
                )
                if message:
                    logging.debug(f"تم استلام رسالة من Redis: {message}")
                    yield f"data: {message['data']}\n\n"
            except Exception as inner_e:
                logging.error(f"خطأ أثناء انتظار رسالة من Redis: {str(inner_e)}", exc_info=True)
            await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"SSE error in event_generator: {str(e)}", exc_info=True)
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(f'payment_{payment_token}')
                logging.info(f"🏁 تم إغلاق اتصال Redis للقناة: payment_{payment_token}")
            except Exception as unsub_e:
                logging.error(f"خطأ أثناء إلغاء الاشتراك من Redis: {str(unsub_e)}", exc_info=True)

@sse_bp.route('/sse')
async def sse_stream():
    payment_token = request.args.get('payment_token')
    telegram_id = request.args.get('telegram_id')

    logging.info(f"طلب SSE وارد بمعلمات: payment_token={payment_token}, telegram_id={telegram_id}")

    if not payment_token or not telegram_id:
        logging.warning("❌ معلمات ناقصة في طلب SSE")
        return jsonify({"error": "المعلمات المطلوبة: payment_token و telegram_id"}), 400

    try:
        telegram_id = int(telegram_id)
    except ValueError:
        logging.error(f"❌ تنسيق Telegram ID غير صالح: {telegram_id}")
        return jsonify({"error": "تنسيق Telegram ID غير صالح"}), 400

    try:
        db_connection = current_app.db_pool
        if not db_connection:
            logging.error("❌ لم يتم تهيئة الاتصال بقاعدة البيانات في التطبيق.")
            return jsonify({"error": "خطأ في الاتصال بقاعدة البيانات"}), 500

        # التحقق من ملكية الدفع باستخدام جدول telegram_payments
        query = '''
            SELECT EXISTS(
                SELECT 1 
                FROM telegram_payments
                WHERE payment_token = $1
                  AND telegram_id = $2
            )
        '''
        is_owner = await db_connection.fetchval(query, payment_token, telegram_id)
        if not is_owner:
            logging.warning(f"❌ وصول غير مصرح به لـ Telegram ID: {telegram_id}")
            return jsonify({"error": "غير مصرح به"}), 403
    except Exception as db_e:
        logging.error(f"❌ خطأ أثناء التحقق من ملكية الدفع: {str(db_e)}", exc_info=True)
        return jsonify({"error": "خطأ داخلي في الخادم أثناء التحقق من الدفع"}), 500

    logging.info(f"✅ جميع المعلمات صالحة، بدء بث SSE لـ payment_token: {payment_token}")
    return Response(
        event_generator(payment_token),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )
