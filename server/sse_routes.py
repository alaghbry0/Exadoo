# server/sse_routes.py
from quart import Blueprint, request, Response, jsonify, current_app
import logging
import json
from datetime import datetime, timedelta
import asyncio
from server.redis_manager import redis_manager

sse_bp = Blueprint('sse', __name__)


async def event_generator(payment_token):
    pubsub = None  # تهيئة المتغير خارج كتلة try
    try:
        pubsub = redis_manager.redis.pubsub()
        await pubsub.subscribe(f'payment_{payment_token}')

        event_buffer = []
        last_sent_seq = -1

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=30
            )

            if message:
                try:
                    data = json.loads(message['data'])
                    event_buffer.append(data)

                    # فرز البافر حسب التسلسل
                    event_buffer.sort(key=lambda x: x.get('_seq', 0))

                    # إرسال الأحداث بالترتيب
                    while event_buffer and event_buffer[0].get('_seq', 0) == last_sent_seq + 1:
                        next_event = event_buffer.pop(0)
                        last_sent_seq = next_event.get('_seq', 0)
                        yield f"data: {json.dumps(next_event)}\n\n"

                except Exception as e:
                    logging.error(f"Error processing message: {str(e)}")

            await asyncio.sleep(0.1)

    except Exception as e:
        logging.error(f"SSE error: {str(e)}", exc_info=True)
    finally:
        if pubsub:  # التحقق من وجود pubsub قبل الإغلاق
            try:
                await pubsub.unsubscribe(f'payment_{payment_token}')
                logging.info(f"تم إلغاء الاشتراك من القناة: payment_{payment_token}")
            except Exception as e:
                logging.error(f"Error during unsubscribe: {str(e)}")

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


@sse_bp.route('/check-payment/<payment_token>')
async def check_payment(payment_token):
    try:
        # التحقق من وجود الدفعة في Redis أو قاعدة البيانات
        payment_data = await redis_manager.redis.get(f'payment_{payment_token}')

        if payment_data:
            return jsonify(json.loads(payment_data)), 200

        # إذا لم توجد في Redis، ابحث في قاعدة البيانات
        async with current_app.db_pool.acquire() as conn:
            record = await conn.fetchrow('''
                SELECT status, created_at 
                FROM payments 
                WHERE payment_token = $1
            ''', payment_token)

            if record:
                return jsonify({
                    'status': record['status'],
                    'created_at': record['created_at'].isoformat()
                }), 200

        return jsonify({'error': 'Payment not found'}), 404

    except Exception as e:
        logging.error(f"Check payment error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500


@sse_bp.route('/verify-payment/<payment_token>')
async def verify_payment(payment_token):
    try:
        async with current_app.db_pool.acquire() as conn:
            # التحقق من وجود الدفعة ومطابقة البيانات
            record = await conn.fetchrow('''
                SELECT 
                    p.telegram_id,
                    p.amount,
                    p.status,
                    tp.plan_id
                FROM payments p
                JOIN telegram_payments tp ON p.payment_token = tp.payment_token
                WHERE p.payment_token = $1
            ''', payment_token)

            if not record:
                return jsonify({'valid': False, 'reason': 'Invalid token'}), 404

            # التحقق من الصلاحية (مثال: 24 ساعة)
            is_expired = datetime.now() - record['created_at'] > timedelta(hours=24)

            return jsonify({
                'valid': not is_expired and record['status'] == 'pending',
                'telegram_id': record['telegram_id'],
                'plan_id': record['plan_id'],
                'amount': record['amount']
            }), 200

    except Exception as e:
        logging.error(f"Verify payment error: {str(e)}")
        return jsonify({'valid': False}), 500