import logging
import json
import os
import asyncio
from aiogram import Router, types
from aiogram.types import Message, SuccessfulPayment, PreCheckoutQuery
from quart import current_app
from database.db_queries import record_payment

# 🔹 إنشاء Router جديد لـ aiogram 3.x
router = Router()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ✅ `RETRY_LIMIT` لتحديد عدد محاولات إعادة الطلب عند فشل الاتصال
RETRY_LIMIT = 3


# 🔹 التحقق من صحة المدفوعات قبل تأكيد الدفع
@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)

        if not payload.get("planId") or not payload.get("userId"):
            logging.error(f"❌ بيانات الدفع غير صحيحة: {payload}")
            await pre_checkout_query.answer(ok=False, error_message="Invalid payment payload")
            return

        # ✅ التحقق من أن total_amount مطابق للسعر المتوقع (لزيادة الأمان)
        expected_price = payload.get("amount", 0)
        if pre_checkout_query.total_amount != expected_price * 100:
            logging.warning(f"⚠️ مبلغ غير متطابق: متوقع {expected_price}, لكن وصل {pre_checkout_query.total_amount / 100}")
            await pre_checkout_query.answer(ok=False, error_message="Price mismatch error")
            return

        logging.info(f"✅ فاتورة جديدة لمستخدم {payload['userId']} بقيمة {pre_checkout_query.total_amount / 100}")
        await pre_checkout_query.answer(ok=True)

    except Exception as e:
        logging.error(f"❌ خطأ أثناء التحقق من الفاتورة: {e}")
        await pre_checkout_query.answer(ok=False, error_message="Internal error")


# 🔹 معالجة الدفع الناجح
@router.message()
async def handle_successful_payment(message: Message):
    if isinstance(message.successful_payment, SuccessfulPayment):  # ✅ التحقق من نوع الرسالة
        try:
            payment = message.successful_payment
            payload = json.loads(payment.invoice_payload)
            telegram_id = payload["userId"]
            plan_id = payload["planId"]
            payment_id = payment.telegram_payment_charge_id
            amount = payment.total_amount // 100  # تحويل النجوم إلى الدولار

            logging.info(f"✅ استلام دفعة جديدة من {telegram_id} للخطة {plan_id}, مبلغ: {amount}")

            # ✅ التحقق من اتصال قاعدة البيانات
            db_pool = getattr(current_app, "db_pool", None)
            if not db_pool:
                logging.error("❌ قاعدة البيانات غير متاحة!")
                return await message.answer("⚠️ خطأ داخلي، يرجى المحاولة لاحقًا.")

            async with db_pool.acquire() as conn:
                # ✅ التحقق من أن الدفع غير مسجل مسبقًا
                existing_payment = await conn.fetchrow("SELECT * FROM payments WHERE payment_id = $1", payment_id)
                if existing_payment:
                    logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                    return await message.answer("✅ تم استلام دفعتك بالفعل!")

                # ✅ تسجيل الدفع في قاعدة البيانات
                await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount, plan_id=plan_id)

            # ✅ إرسال طلب إلى API Next.js لتجديد الاشتراك
            success = await send_subscription_request(telegram_id, plan_id, payment_id)

            if success:
                return await message.answer("🎉 تم تفعيل اشتراكك بنجاح!")
            else:
                return await message.answer("⚠️ حدث خطأ أثناء تجديد الاشتراك، يرجى التواصل مع الدعم.")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء معالجة الدفع: {e}")
            await message.answer("⚠️ حدث خطأ أثناء معالجة الدفع، يرجى المحاولة لاحقًا.")


async def send_subscription_request(telegram_id, plan_id, payment_id):
    """
    🔁 دالة لإرسال طلب تجديد الاشتراك مع `Retry` في حالة الفشل.
    ✅ يتم استخدام `current_app.aiohttp_session` بدلاً من إنشاء جلسة جديدة.
    """
    subscribe_url = "https://exadoo.onrender.com/api/subscribe"
    webhook_secret = os.getenv("WEBHOOK_SECRET")

    session = getattr(current_app, "aiohttp_session", None)
    if not session or session.closed:
        logging.critical("❌ جلسة aiohttp غير صالحة!")
        return False

    payload = {
        "telegram_id": telegram_id,
        "subscription_type_id": plan_id,
        "payment_id": payment_id
    }
    headers = {"Authorization": f"Bearer {webhook_secret}"}

    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            async with session.post(subscribe_url, json=payload, headers=headers) as resp:
                response_text = await resp.text()

                if resp.status == 200:
                    logging.info(f"✅ تم تجديد الاشتراك بنجاح للمستخدم {telegram_id}")
                    return True
                else:
                    logging.error(f"❌ فشل تجديد الاشتراك، المحاولة {attempt}/{RETRY_LIMIT}: {response_text}")

        except Exception as e:
            logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt}/{RETRY_LIMIT}: {e}")

        if attempt < RETRY_LIMIT:
            await asyncio.sleep(2 ** attempt)  # ⏳ انتظار قبل إعادة المحاولة

    logging.critical(f"🚨 جميع محاولات تجديد الاشتراك للمستخدم {telegram_id} فشلت!")
    return False
