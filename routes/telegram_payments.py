from uuid import uuid4
from asyncpg.exceptions import UniqueViolationError
from quart import current_app, jsonify, request
import logging


@payment_bp.route("/api/create-telegram-payment-token", methods=["POST"])
async def create_telegram_payment_token():
    """
    إنشاء رمز دفع فريد لمعاملات Telegram Stars مع إدارة التكرار وإعادة المحاولة
    """
    max_attempts = 3
    attempt = 0

    try:
        data = await request.get_json()
        telegram_id = data.get('telegramId')
        plan_id = data.get('planId')

        # التحقق من البيانات الأساسية
        if not all([telegram_id, plan_id]):
            logging.error("❌ بيانات الطلب ناقصة: telegramId أو planId")
            return jsonify({
                "error": "البيانات المطلوبة ناقصة",
                "required_fields": ["telegramId", "planId"]
            }), 400

        async with current_app.db_pool.acquire() as conn:
            while attempt < max_attempts:
                payment_token = str(uuid4())
                try:
                    # محاولة الإدراج في قاعدة البيانات
                    result = await conn.execute('''
                        INSERT INTO payments (
                            payment_token,
                            telegram_id,
                            subscription_plan_id,
                            payment_method,
                            status,
                            payment_date,
                            created_at
                        ) VALUES (
                            $1, $2, $3, $4, 'pending',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        RETURNING payment_token
                    ''', payment_token, telegram_id, plan_id, 'telegram_stars')

                    if result:
                        logging.info(f"✅ تم إنشاء رمز الدفع: {payment_token}")
                        return jsonify({
                            "payment_token": payment_token,
                            "retries": attempt + 1
                        }), 200

                except UniqueViolationError as uve:
                    logging.warning(f"⚠️ تكرار في رمز الدفع (محاولة {attempt + 1}/{max_attempts}): {uve}")
                    attempt += 1
                    if attempt >= max_attempts:
                        raise Exception("فشل إنشاء رمز دفع فريد بعد 3 محاولات")
                    continue

                except Exception as e:
                    logging.error(f"❌ خطأ غير متوقع: {str(e)}")
                    raise

            return jsonify({"error": "فشل إنشاء رمز الدفع"}), 500

    except Exception as e:
        logging.error(f"🚨 فشل حرج في إنشاء رمز الدفع: {str(e)}")
        return jsonify({
            "error": "خطأ داخلي في الخادم",
            "details": str(e)
        }), 500