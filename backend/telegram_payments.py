import logging
import json
import aiohttp
import os
import asyncio
from aiogram.filter import ContentTypesFilter
from aiogram.enums import ContentType
from aiogram import Router, types
from aiogram.types import Message, PreCheckoutQuery
from aiogram.enums import ContentType
from aiogram.utils.web_app import check_webapp_signature
from quart import current_app
from database.db_queries import record_payment

# 🔹 إنشاء Router جديد لـ aiogram 3.x
router = Router()

# 🔹 إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# 🔹 التحقق من صحة المدفوعات قبل تأكيد الدفع
@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)

        if not payload.get("planId") or not payload.get("userId"):
            logging.error(f"❌ بيانات الدفع غير صحيحة: {payload}")
            await pre_checkout_query.answer(ok=False, error_message="Invalid payload")
            return

        logging.info(f"✅ فاتورة جديدة لمستخدم {payload['userId']} بقيمة {pre_checkout_query.total_amount}")
        await pre_checkout_query.answer(ok=True)

    except Exception as e:
        logging.error(f"❌ خطأ أثناء التحقق من الفاتورة: {e}")
        await pre_checkout_query.answer(ok=False, error_message="Internal error")


# 🔹 معالجة الدفع الناجح
@router.message(ContentTypeFilter(content_types=[ContentType.SUCCESSFUL_PAYMENT]))
async def handle_successful_payment(message: Message):
    if not message.successful_payment:
        return

    try:
        payment = message.successful_payment
        payload = json.loads(payment.invoice_payload)
        telegram_id = payload["userId"]
        plan_id = payload["planId"]
        payment_id = payment.telegram_payment_charge_id
        amount = payment.total_amount // 100  # تحويل النجوم إلى دولار

        logging.info(f"✅ استلام دفعة جديدة من {telegram_id} للخطة {plan_id}, مبلغ: {amount}")

        # ✅ الاتصال بقاعدة البيانات
        db_pool = current_app.db_pool if hasattr(current_app, "db_pool") else None
        if not db_pool:
            logging.error("❌ قاعدة البيانات غير متاحة!")
            return await message.answer("⚠️ خطأ داخلي، يرجى المحاولة لاحقًا.")

        async with db_pool.acquire() as conn:
            # 🔹 التحقق من أن الدفع لم يتم تسجيله مسبقًا
            existing_payment = await conn.fetchrow(
                "SELECT * FROM payments WHERE payment_id = $1", payment_id
            )
            if existing_payment:
                logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                return await message.answer("✅ تم استلام دفعتك بالفعل!")

            # 🔹 تسجيل الدفع في قاعدة البيانات
            await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount, plan_id=plan_id)

        # 🔹 إرسال طلب إلى API Next.js لتجديد الاشتراك
        subscribe_url = "https://exadoo.onrender.com/api/subscribe"
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        max_retries = 3

        async with aiohttp.ClientSession() as session:
            for attempt in range(max_retries):  # 🔁 المحاولة حتى 3 مرات في حالة الفشل
                try:
                    async with session.post(
                            subscribe_url,
                            json={"telegram_id": telegram_id, "subscription_type_id": plan_id,
                                  "payment_id": payment_id},
                            headers={"Authorization": f"Bearer {webhook_secret}"}
                    ) as resp:
                        if resp.status == 200:
                            logging.info(f"✅ تم تجديد الاشتراك بنجاح للمستخدم {telegram_id}")
                            return await message.answer("🎉 تم تفعيل اشتراكك بنجاح!")
                        else:
                            error_message = await resp.text()
                            logging.error(f"❌ فشل تجديد الاشتراك، المحاولة {attempt + 1}: {error_message}")

                    # انتظار قبل إعادة المحاولة
                    await asyncio.sleep(2)

                except Exception as e:
                    logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt + 1}: {e}")
                    await asyncio.sleep(2)

        await message.answer("⚠️ حدث خطأ أثناء تجديد الاشتراك، يرجى التواصل مع الدعم.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة الدفع: {e}")
        await message.answer("⚠️ حدث خطأ أثناء معالجة الدفع، يرجى المحاولة لاحقًا.")
