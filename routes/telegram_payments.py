from uuid import uuid4
from asyncpg.exceptions import UniqueViolationError
from quart import Blueprint, current_app, jsonify, request
import logging

payment_bp = Blueprint("payment", __name__)

@payment_bp.route("/api/create-telegram-payment-token", methods=["POST"])
async def create_telegram_payment_token():
    """
    إنشاء رمز دفع فريد لمعاملات Telegram Stars وتخزينه مؤقتاً في جدول telegram_payments.
    يتم حذف البيانات التي مضى عليها أكثر من ساعتين.
    """
    max_attempts = 3
    attempt = 0

    try:
        data = await request.get_json()
        telegram_id_raw = data.get('telegramId')
        # في حال أردت الاستمرار بتلقي planId مع أن الجدول الجديد لا يخزنه


        # التحقق من وجود telegramId فقط لأننا نستخدمه فقط في الجدول الجديد
        if not telegram_id_raw:
            logging.error("❌ بيانات الطلب ناقصة: telegramId")
            return jsonify({
                "error": "البيانات المطلوبة ناقصة",
                "required_fields": ["telegramId"]
            }), 400

        try:
            telegram_id = int(telegram_id_raw)
        except ValueError:
            logging.error("❌ قيمة telegramId غير صالحة: يجب أن تكون رقمًا")
            return jsonify({
                "error": "قيمة telegramId غير صالحة: يجب أن تكون رقمًا"
            }), 400

        async with current_app.db_pool.acquire() as conn:
            # حذف السجلات التي مضى عليها أكثر من ساعتين (حذف البيانات المؤقتة)
            await conn.execute('''
                DELETE FROM telegram_payments
                WHERE created_at < NOW() - INTERVAL '2 hours'
            ''')

            while attempt < max_attempts:
                payment_token = str(uuid4())
                try:
                    # إدراج البيانات في جدول telegram_payments فقط
                    result = await conn.execute('''
                        INSERT INTO telegram_payments (
                            payment_token,
                            telegram_id,
                            status,
                            created_at
                        ) VALUES (
                            $1, $2, 'pending', CURRENT_TIMESTAMP
                        )
                        RETURNING payment_token
                    ''', payment_token, telegram_id)

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
