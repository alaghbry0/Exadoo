from uuid import uuid4
from asyncpg.exceptions import UniqueViolationError
from quart import Blueprint, current_app, jsonify, request
import logging

payment_bp = Blueprint("payment", __name__)

@payment_bp.route("/api/create-telegram-payment-token", methods=["POST"])
async def create_telegram_payment_token():
    """
    إنشاء رمز دفع فريد لمعاملات Telegram Stars مع إدارة التكرار وإعادة المحاولة
    """
    max_attempts = 3
    attempt = 0

    try:
        data = await request.get_json()
        telegram_id_raw = data.get('telegramId')
        plan_id = data.get('planId')

        # التحقق من البيانات الأساسية
        if not all([telegram_id_raw, plan_id]):
            logging.error("❌ بيانات الطلب ناقصة: telegramId أو planId")
            return jsonify({
                "error": "البيانات المطلوبة ناقصة",
                "required_fields": ["telegramId", "planId"]
            }), 400

        try:
            # تحويل telegramId إلى عدد صحيح إذا كان ذلك مناسبًا
            telegram_id = int(telegram_id_raw)
        except ValueError:
            logging.error("❌ قيمة telegramId غير صالحة: يجب أن تكون رقمًا")
            return jsonify({
                "error": "قيمة telegramId غير صالحة: يجب أن تكون رقمًا"
            }), 400

        # إذا كان من المفترض استخدام telegram_id كـ user_id، يمكن تعيينه كذلك
        user_id = telegram_id

        async with current_app.db_pool.acquire() as conn:
            while attempt < max_attempts:
                payment_token = str(uuid4())
                try:
                    # تعديل الاستعلام لإدخال قيمة user_id
                    result = await conn.execute('''
                        INSERT INTO payments (
                            payment_token,
                            user_id,
                            telegram_id,
                            subscription_plan_id,
                            payment_method,
                            status,
                            payment_date,
                            created_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, 'pending',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        RETURNING payment_token
                    ''', payment_token, user_id, telegram_id, plan_id, 'telegram_stars')

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
