from aiogram import Bot, Dispatcher, types
import logging
import json
import aiohttp
from database.db_queries import record_payment


async def handle_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)
        # التحقق الأساسي من البيانات
        if payload.get('planId') and payload.get('userId'):
            await pre_checkout_query.answer(ok=True)
        else:
            await pre_checkout_query.answer(ok=False, error_message="Invalid payload")
    except Exception as e:
        logging.error(f"Pre-checkout error: {e}")
        await pre_checkout_query.answer(ok=False, error_message="Internal error")


async def handle_successful_payment(message: types.Message):
    try:
        payment = message.successful_payment
        payload = json.loads(payment.invoice_payload)

        # تسجيل الدفع في قاعدة البيانات
        async with message.bot.db_pool.acquire() as conn:
            await record_payment(
                conn,
                user_id=payload['userId'],
                payment_id=payment.telegram_payment_charge_id,
                amount=payment.total_amount // 100,  # تحويل النجوم إلى دولار
                plan_id=payload['planId']
            )

        # إرسال طلب إلى Next.js لتجديد الاشتراك
        async with aiohttp.ClientSession() as session:
            await session.post(
                "https://yourdomain.com/api/subscribe",
                json={
                    "telegram_id": payload['userId'],
                    "subscription_type_id": payload['planId'],
                    "payment_id": payment.telegram_payment_charge_id
                }
            )

        await message.answer("✅ تمت عملية الدفع بنجاح! سيتم تفعيل اشتراكك فورًا.")
    except Exception as e:
        logging.error(f"Payment processing failed: {e}")
        await message.answer("❌ حدث خطأ أثناء معالجة الدفع، يرجى التواصل مع الدعم.")


def setup_payment_handlers(dp: Dispatcher):
    dp.register_pre_checkout_query_handler(handle_pre_checkout)
    dp.register_message_handler(handle_successful_payment, content_types=types.ContentType.SUCCESSFUL_PAYMENT)