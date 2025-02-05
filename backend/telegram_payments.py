import logging
import json
import os
import asyncio
from aiogram import Router, types
from aiogram.types import Message, SuccessfulPayment
from quart import current_app
from database.db_queries import record_payment

# 🔹 إعدادات الـ Router لـ aiogram
router = Router()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ✅ رابط Webhook لمعالجة المدفوعات
WEBHOOK_URL = "http://127.0.0.1:5000/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ✅ تحميل `WEBHOOK_SECRET`


@router.message()
async def handle_successful_payment(message: Message):
    """🔹 معالجة الدفع الناجح"""
    if isinstance(message.successful_payment, SuccessfulPayment):
        try:
            payment = message.successful_payment
            payload = json.loads(payment.invoice_payload)

            # ✅ التحقق من البيانات المستلمة
            telegram_id = payload.get("userId")
            plan_id = payload.get("planId")
            payment_id = payment.telegram_payment_charge_id
            amount = payment.total_amount // 100  # تحويل النجوم إلى الدولار

            if not isinstance(telegram_id, int) or not isinstance(plan_id, int) or not isinstance(payment_id, str):
                logging.error(
                    f"❌ بيانات الدفع غير صالحة! telegram_id={telegram_id}, plan_id={plan_id}, payment_id={payment_id}")
                return await message.answer("⚠️ بيانات الدفع غير صحيحة، يرجى التواصل مع الدعم.")

            logging.info(f"✅ استلام دفعة جديدة من {telegram_id} للخطة {plan_id}, مبلغ: {amount}")

            # ✅ تسجيل الدفع في قاعدة البيانات
            db_pool = getattr(current_app, "db_pool", None)
            if not db_pool:
                logging.error("❌ قاعدة البيانات غير متاحة!")
                return await message.answer("⚠️ خطأ داخلي، يرجى المحاولة لاحقًا.")

            async with db_pool.acquire() as conn:
                existing_payment = await conn.fetchrow("SELECT * FROM payments WHERE payment_id = $1", payment_id)
                if existing_payment:
                    logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                    return await message.answer("✅ تم استلام دفعتك بالفعل!")

                await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount,
                                     subscription_type_id=plan_id)

            # ✅ إرسال الطلب إلى `/webhook`
            success = await send_to_webhook(telegram_id, plan_id, payment_id)

            if success:
                return await message.answer("✅ تم استلام الدفع بنجاح! سيتم تفعيل اشتراكك قريبًا.")
            else:
                return await message.answer("⚠️ حدث خطأ أثناء إرسال الدفع، يرجى التواصل مع الدعم.")

        except json.JSONDecodeError as e:
            logging.error(f"❌ خطأ في تحليل بيانات الفاتورة: {e}")
            await message.answer("⚠️ بيانات الدفع غير صالحة، يرجى التواصل مع الدعم.")
        except Exception as e:
            logging.error(f"❌ خطأ أثناء معالجة الدفع: {e}")
            await message.answer("⚠️ حدث خطأ أثناء معالجة الدفع، يرجى المحاولة لاحقًا.")


async def send_to_webhook(telegram_id, plan_id, payment_id, max_retries=3):
    """🔁 إرسال بيانات الدفع إلى `/webhook` مع `Retry` في حالة الفشل"""

    session = getattr(current_app, "aiohttp_session", None)
    if not session or session.closed:
        logging.critical("❌ جلسة aiohttp غير صالحة!")
        return False

    payload = {
        "telegram_id": telegram_id,
        "subscription_type_id": plan_id,
        "payment_id": payment_id
    }
    headers = {
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET  # ✅ إرسال `WEBHOOK_SECRET` لضمان أمان الطلبات
    }

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"🚀 إرسال طلب الدفع إلى {WEBHOOK_URL} - المحاولة {attempt}/{max_retries}")
            async with session.post(WEBHOOK_URL, json=payload, headers=headers) as resp:
                response_text = await resp.text()
                logging.info(f"🔹 استجابة Webhook: {resp.status} - {response_text}")

                if resp.status == 200:
                    logging.info("✅ تم إرسال الدفع بنجاح!")
                    return True
                else:
                    logging.error(f"❌ فشل إرسال الدفع، المحاولة {attempt}/{max_retries}: {response_text}")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء الاتصال بـ Webhook، المحاولة {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # ⏳ انتظار متزايد بين المحاولات (2s, 4s, 8s)

    logging.critical("🚨 جميع محاولات إرسال الدفع فشلت!")
    return False
