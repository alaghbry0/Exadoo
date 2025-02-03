import logging
import json
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.utils.web_app import check_webapp_signature
from database.db_queries import record_payment
from quart import current_app

# ✅ تحسين تسجيل الأخطاء والعمليات
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def handle_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    """معالجة الفاتورة قبل الدفع"""
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


async def handle_successful_payment(message: types.Message):
    """معالجة الدفع الناجح"""
    try:
        payment = message.successful_payment
        payload = json.loads(payment.invoice_payload)
        telegram_id = payload["userId"]
        plan_id = payload["planId"]
        payment_id = payment.telegram_payment_charge_id
        amount = payment.total_amount // 100  # تحويل النجوم إلى دولار

        logging.info(f"✅ دفع ناجح من المستخدم {telegram_id}, plan_id: {plan_id}, amount: {amount}")

        db_pool = current_app.db_pool if hasattr(current_app, "db_pool") else None
        if not db_pool:
            logging.error("❌ قاعدة البيانات غير متاحة!")
            return await message.answer("⚠️ خطأ داخلي، يرجى المحاولة لاحقًا.")

        # 🔹 التحقق من أن الدفع لم يتم تسجيله مسبقًا
        async with db_pool.acquire() as conn:
            existing_payment = await conn.fetchrow(
                "SELECT * FROM payments WHERE payment_id = $1", payment_id
            )
            if existing_payment:
                logging.warning(f"⚠️ الدفع مسجل مسبقًا: {payment_id}")
                return await message.answer("✅ تم استلام دفعتك بالفعل!")

            # 🔹 تسجيل الدفع في قاعدة البيانات
            await record_payment(conn, user_id=telegram_id, payment_id=payment_id, amount=amount, plan_id=plan_id)

        # 🔹 إرسال طلب إلى API Next.js لتجديد الاشتراك
        for attempt in range(3):  # 🔁 المحاولة حتى 3 مرات في حالة الفشل
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(
                        "https://yourdomain.com/api/subscribe",
                        json={"telegram_id": telegram_id, "subscription_type_id": plan_id, "payment_id": payment_id}
                    ) as resp:
                        if resp.status == 200:
                            logging.info(f"✅ تم تجديد الاشتراك بنجاح للمستخدم {telegram_id}")
                            return await message.answer("🎉 تم تفعيل اشتراكك بنجاح!")
                        else:
                            logging.error(f"❌ فشل تجديد الاشتراك، المحاولة {attempt+1}: {await resp.text()}")

                except Exception as e:
                    logging.error(f"❌ خطأ أثناء الاتصال بـ API الاشتراك، المحاولة {attempt+1}: {e}")

        await message.answer("⚠️ حدث خطأ أثناء تجديد الاشتراك، يرجى التواصل مع الدعم.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة الدفع: {e}")
        await message.answer("⚠️ حدث خطأ أثناء معالجة الدفع، يرجى المحاولة لاحقًا.")


def setup_payment_handlers(dp: Dispatcher):
    """تسجيل معالجات الدفع في Aiogram"""
    dp.pre_checkout_query.register(handle_pre_checkout)
    dp.message.register(handle_successful_payment, content_types=types.ContentType.SUCCESSFUL_PAYMENT)
