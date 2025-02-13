# payment_confirmation.py (محدث لمعالجة telegramId غير الصالح و payment_status الـ None)
import logging
from quart import Blueprint, request, jsonify, current_app
import json
from database.db_queries import record_payment, fetch_pending_payment_by_wallet

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API مُدمجة لتأكيد استلام الدفع ومعالجة بيانات المستخدم.
    تسمح بتسجيل دفعات معلقة جديدة لنفس عنوان المحفظة إذا كانت الدفعة السابقة مكتملة،
    وتمنع التسجيل إذا كانت الدفعة السابقة معلقة بالفعل.
    """

    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment (مدمجة): {json.dumps(data, indent=2)}")

        # استلام البيانات (كما هو)
        user_wallet_address = data.get("userWalletAddress")
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")

        # التحقق من البيانات الأساسية (كما هو)
        if not all([user_wallet_address, plan_id_str, telegram_id_str]):
            logging.error("❌ بيانات تأكيد الدفع غير مكتملة!")
            return jsonify({"error": "Invalid payment confirmation data"}), 400

        logging.info(
            f"✅ استلام طلب تأكيد الدفع (مدمج): userWalletAddress={user_wallet_address}, planId={plan_id_str}, "
            f"telegram_id={telegram_id_str}, username={telegram_username}, full_name={full_name}"
        )

        amount = 0

        # ✅ التحقق مما إذا كان telegram_id_str رقمًا صحيحًا صالحًا
        if not telegram_id_str.isdigit(): # ✅ فحص باستخدام isdigit()
            logging.error(f"❌ telegramId غير صالح: {telegram_id_str}. يجب أن يكون رقمًا صحيحًا.")
            return jsonify({"error": "Invalid telegramId. Must be a valid integer."}), 400 # ✅ إرجاع استجابة خطأ 400
        telegram_id = int(telegram_id_str) # ✅ التحويل إلى int فقط إذا كان صالحًا


        try:
            subscription_type_id = int(plan_id_str)
        except ValueError:
            subscription_type_id = 3
            logging.warning(f"⚠️ Plan ID '{plan_id_str}' is not a valid integer. Using default subscription type ID: {subscription_type_id}")
        except TypeError:
            subscription_type_id = 3
            logging.warning(f"⚠️ Plan ID is missing in request. Using default subscription type ID: {subscription_type_id}")

        # ✅ البحث عن دفعة موجودة لنفس عنوان المحفظة (بغض النظر عن الحالة)
        async with current_app.db_pool.acquire() as conn:
            existing_payment = await fetch_pending_payment_by_wallet(conn, user_wallet_address) # ✅ استخدام fetch_payment_by_wallet بدون تصفية الحالة

        if existing_payment:
            # ✅ فحص حالة الدفعة الموجودة
            payment_status = existing_payment.get('status')
            if payment_status is not None and payment_status.lower() == "pending": # ✅ التحقق من الحالة "pending" (بغض النظر عن حالة الأحرف) و فحص None
                logging.warning(f"⚠️ دفعة معلقة موجودة بالفعل لنفس عنوان المحفظة: {user_wallet_address}")
                return jsonify({"error": "Pending payment already exists for this wallet address"}), 409 # ✅ رمز حالة 409 Conflict
            elif payment_status is not None and payment_status.lower() == "completed": # ✅ السماح بالدفع الجديد إذا كانت الحالة "completed" و فحص None
                logging.info(f"ℹ️ دفعة مكتملة موجودة بالفعل لنفس عنوان المحفظة: {user_wallet_address}. السماح بتسجيل دفعة معلقة جديدة.")
                # ✅ هنا، سنسمح بتسجيل دفعة معلقة جديدة حتى لو كانت هناك دفعة مكتملة سابقة
            elif payment_status is not None: # ✅ فحص None لحالات الدفع الأخرى
                logging.info(f"ℹ️ دفعة موجودة بحالة أخرى ({payment_status}) لنفس عنوان المحفظة: {user_wallet_address}. السماح بتسجيل دفعة معلقة جديدة.")
                # ✅ يمكنك إضافة حالات أخرى هنا إذا كنت تريد معالجة حالات الدفع الأخرى بشكل مختلف
            else:
                logging.warning(f"⚠️ حالة الدفعة الموجودة غير معروفة (None) لعنوان المحفظة: {user_wallet_address}. السماح بتسجيل دفعة معلقة جديدة على أي حال.")
                # ✅ معالجة حالة status=None بشكل صريح - يمكنك تعديل هذا السلوك إذا كنت ترغب في رفض الدفعات في حالة status=None

        # تسجيل دفعة معلقة جديدة (كما هو)
        async with current_app.db_pool.acquire() as conn:
            await record_payment(conn, telegram_id, user_wallet_address, amount, subscription_type_id, username=telegram_username, full_name=full_name) # ✅ استدعاء record_payment بدون payment_id

        logging.info(
            f"💾 تسجيل بيانات الدفع والمستخدم كدفعة معلقة: userWalletAddress={user_wallet_address}, "
            f"planId={plan_id_str}, telegram_id={telegram_id}, subscription_type_id={subscription_type_id}, username={telegram_username}, full_name={full_name}"
        )

        return jsonify({"message": "Payment confirmation and user data received and pending"}), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment (مدمجة): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500