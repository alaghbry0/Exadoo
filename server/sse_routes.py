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

    # التحقق من وجود المعلمات
    if not payment_token or not telegram_id:
        logging.warning("❌ معلمات ناقصة في طلب SSE")
        return jsonify({"error": "المعلمات المطلوبة: payment_token و telegram_id"}), 400

    # التحقق من صحة Telegram ID
    try:
        telegram_id = int(telegram_id)
    except ValueError:
        logging.error(f"❌ تنسيق Telegram ID غير صالح: {telegram_id}")
        return jsonify({"error": "تنسيق Telegram ID غير صالح"}), 400

    # التحقق من ملكية الدفع باستخدام الاتصال بقاعدة البيانات
    # ملاحظة: تأكد من أن الاتصال بقاعدة البيانات متاح تحت request.app.db أو قم بتعديل الاسم إلى request.app.db_pool
    try:
        db_connection = request.app.db  # تأكد هنا من الاسم الصحيح (ربما يجب أن يكون request.app.db_pool)
        if not db_connection:
            logging.error("❌ لم يتم تهيئة الاتصال بقاعدة البيانات في التطبيق.")
            return jsonify({"error": "خطأ في الاتصال بقاعدة البيانات"}), 500

        is_owner = await validate_payment_owner(db_connection, payment_token, telegram_id)
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
