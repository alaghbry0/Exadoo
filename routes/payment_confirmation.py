# payment_confirmation.py (modified - corrected telegram_id data type and URL conversion)
import logging
from quart import Blueprint, request, jsonify, current_app
import json
from database.db_queries import record_payment
import os  # ✅ استيراد مكتبة os
import aiohttp  # ✅ استيراد مكتبة aiohttp

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)
WEBHOOK_SECRET_BACKEND = os.getenv("WEBHOOK_SECRET")  # ✅ تحميل WEBHOOK_SECRET للخادم
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL", "http://localhost:5000/api/subscribe")

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API لتأكيد استلام الدفع ومعالجة بيانات المستخدم.
    تسجل دفعة معلقة جديدة دون التحقق من وجود دفعات معلقة سابقة.
    """
    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}")

        # ✅ التحقق من WEBHOOK_SECRET المرسل من الواجهة الأمامية
        webhook_secret_frontend = data.get("webhookSecret")
        if not webhook_secret_frontend or webhook_secret_frontend != WEBHOOK_SECRET_BACKEND:
            logging.warning("❌ طلب غير مصرح به إلى /api/confirm_payment: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403  # إرجاع رمز حالة 403 للرفض

        # استخراج البيانات الأساسية من الطلب (بقية البيانات كما هي)
        user_wallet_address = data.get("userWalletAddress")
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")  # ✅ تصحيح: استخدام "telegramId" بحرف 'I' كبير
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")

        # ... (بقية التحقق من صحة البيانات الأساسية كما هي)

        logging.info(
            f"✅ استلام طلب تأكيد الدفع: userWalletAddress={user_wallet_address}, "
            f"planId={plan_id_str}, telegramId={telegram_id_str}, username={telegram_username}, full_name={full_name}"
        )

        amount = 0

        # ✅ معالجة plan_id_str وتعريف subscription_type_id
        try:
            plan_id = int(plan_id_str)
            if plan_id == 1:
                subscription_type_id = 1  # Basic plan
            elif plan_id == 2:
                subscription_type_id = 2  # Premium plan
            else:
                subscription_type_id = 1  # Default to Basic plan if plan_id غير صالح
                logging.warning(f"⚠️ planId غير صالح: {plan_id_str}. تم استخدام الخطة الأساسية افتراضيًا.")
        except ValueError:
            subscription_type_id = 1  # Default to Basic plan if plan_id_str ليس عددًا صحيحًا
            logging.warning(f"⚠️ planId ليس عددًا صحيحًا: {plan_id_str}. تم استخدام الخطة الأساسية افتراضيًا.")

        # ✅ تحويل telegram_id_str إلى عدد صحيح
        try:
            telegram_id = int(telegram_id_str)  # ✅ تحويل telegram_id_str إلى عدد صحيح
        except ValueError:
            logging.error(f"❌ telegramId ليس عددًا صحيحًا: {telegram_id_str}. تعذر تسجيل الدفعة.")
            return jsonify({"error": "Invalid telegramId", "details": "telegramId must be an integer."}), 400  # إرجاع رمز حالة 400 لطلب غير صالح

        # تسجيل دفعة معلقة جديدة دون التحقق من وجود دفعة سابقة (كما هي)
        async with current_app.db_pool.acquire() as conn:
            result = await record_payment(
                conn,
                telegram_id,  # ✅ استخدام telegram_id (عدد صحيح)
                user_wallet_address,
                amount,
                subscription_type_id,  # ✅ الآن subscription_type_id مُعرّف
                username=telegram_username,
                full_name=full_name
            )

        if result:
            logging.info(
                f"💾 تم تسجيل بيانات الدفع والمستخدم كدفعة معلقة: userWalletAddress={user_wallet_address}, "
                f"planId={plan_id_str}, telegramId={telegram_id}, subscription_type_id={subscription_type_id}, "
                f"username={telegram_username}, full_name={full_name}"
            )

            # ✅ استدعاء نقطة نهاية /api/subscribe لتجديد الاشتراك
            async with aiohttp.ClientSession() as session:  # ✅ تأكد من وجود استيراد aiohttp في الملف
                headers = {
                    "Authorization": f"Bearer {WEBHOOK_SECRET_BACKEND}",  # ✅ استخدام مفتاح الخلفية للتوثيق الداخلي
                    "Content-Type": "application/json"
                }
                subscription_payload = {
                    "telegram_id": telegram_id,  # ✅ استخدام telegram_id (عدد صحيح)
                    "subscription_type_id": subscription_type_id,  # ✅ الآن subscription_type_id مُعرّف
                    "payment_id": "manual_confirmation_" + user_wallet_address,  # ✅ إنشاء payment_id فريد للتأكيد اليدوي
                    "username": telegram_username,
                    "full_name": full_name,
                    # لا يتم تضمين بيانات Webhook هنا لأننا لا نستخدم Webhook في هذا التدفق
                }

                logging.info(f"📞 استدعاء /api/subscribe لتجديد الاشتراك: {json.dumps(subscription_payload, indent=2)}")

                # ✅ استخدام str() لتحويل عنوان URL بشكل صريح
                subscribe_api_url = str(current_app.config.get("SUBSCRIBE_API_URL"))

                async with session.post(subscribe_api_url, json=subscription_payload, headers=headers) as response:  # ✅ استخدام عنوان URL من config
                    subscribe_response = await response.json()
                    if response.status == 200:
                        logging.info(f"✅ تم استدعاء /api/subscribe بنجاح! الاستجابة: {subscribe_response}")
                        return jsonify({"message": "Payment confirmation and subscription update initiated successfully"}), 200
                    else:
                        logging.error(f"❌ فشل استدعاء /api/subscribe! الحالة: {response.status}, التفاصيل: {subscribe_response}")
                        return jsonify({"error": "Failed to initiate subscription update", "subscribe_error": subscribe_response}), response.status

        else:
            logging.error("❌ فشل تسجيل الدفعة في قاعدة البيانات.")
            return jsonify({"error": "Failed to record payment"}), 500

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500