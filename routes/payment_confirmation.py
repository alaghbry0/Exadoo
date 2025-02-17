# payment_confirmation.py (modified - with enhanced logging and orderId/sender wallet matching)
import uuid
import logging
import asyncio  # ✅ استيراد asyncio
from quart import Blueprint, request, jsonify, current_app
import json
import os
import aiohttp
from database.db_queries import record_payment, update_payment_with_txhash, fetch_pending_payment_by_wallet
from pytoniq import LiteBalancer, begin_cell, Address  # ✅ استيراد pytoniq

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)
WEBHOOK_SECRET_BACKEND = os.getenv("WEBHOOK_SECRET")
subscribe_api_url = os.getenv("SUBSCRIBE_API_URL")
BOT_WALLET_ADDRESS = os.getenv("BOT_WALLET_ADDRESS")  # ✅ تحميل عنوان محفظة البوت من متغير البيئة
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY") # ✅ تحميل مفتاح API لـ Toncenter

async def parse_transactions(provider: LiteBalancer): # ✅ دالة parse_transactions لمعالجة المعاملات
    """
    تقوم هذه الدالة بجلب آخر المعاملات من محفظة البوت، وفك تشفير الحمولة المخصصة،
    ومطابقة orderId وعنوان محفظة المرسل مع الدفعات المعلقة في قاعدة البيانات، وتحديث حالة الدفع وتجديد الاشتراك.
    """
    logging.info("🔄 بدء parse_transactions...") # ✅ تسجيل بداية الدالة
    my_wallet_address = BOT_WALLET_ADDRESS  # ✅ استخدام عنوان محفظة البوت من متغير البيئة
    if not my_wallet_address:
        logging.error("❌ لم يتم تعريف BOT_WALLET_ADDRESS في متغيرات البيئة!")
        logging.info("❌ إنهاء parse_transactions بسبب عدم تعريف BOT_WALLET_ADDRESS.") # ✅ تسجيل سبب الإنهاء
        return

    logging.info(f"🔍 جلب آخر المعاملات من محفظة البوت: {my_wallet_address}") # ✅ تسجيل المحفظة التي يتم فحصها
    try:
        transactions = await provider.get_transactions(address=my_wallet_address, count=5) # جلب آخر 5 معاملات
        logging.info(f"✅ تم جلب {len(transactions)} معاملة.") # ✅ تسجيل عدد المعاملات التي تم جلبها

        for transaction in transactions:
            tx_hash_hex = transaction.cell.hash.hex() # الحصول على tx_hash لكل معاملة
            logging.info(f"🔄 فحص المعاملة tx_hash: {tx_hash_hex}") # ✅ تسجيل tx_hash للمعاملة الحالية

            if not transaction.in_msg.is_internal:
                logging.info(f"➡️ معاملة واردة tx_hash: {tx_hash_hex} ليست داخلية - تم تجاهلها.")
                continue
            if transaction.in_msg.info.dest.to_str(1, 1, 1) != my_wallet_address:
                logging.info(f"➡️ معاملة واردة tx_hash: {tx_hash_hex} ليست موجهة إلى محفظة البوت - تم تجاهلها.")
                continue

            sender_wallet_address = transaction.in_msg.info.src.to_str(1, 1, 1)
            value = transaction.in_msg.info.value_coins
            logging.info(f"💰 معاملة داخلية واردة tx_hash: {tx_hash_hex} من {sender_wallet_address} بقيمة {value / 1e9} TON.") # ✅ تسجيل تفاصيل المعاملة الداخلية

            if len(transaction.in_msg.body.bits) < 32:
                logging.info(f"ℹ️ معاملة TON عادية tx_hash: {tx_hash_hex} من {sender_wallet_address} بقيمة {value / 1e9} TON - تم تجاهلها (أقل من 32 بت في الجسم).")
                continue

            body_slice = transaction.in_msg.body.begin_parse()
            op_code = body_slice.load_uint(32)
            logging.info(f"⚙️ رمز العملية (OP Code) للمعاملة tx_hash: {tx_hash_hex} هو {op_code}") # ✅ تسجيل OP Code

            if op_code != 0xf8a7ea5:  # OP Code لتحويل Jetton
                logging.info(f"⚠️ معاملة tx_hash: {tx_hash_hex} ليست Jetton Transfer Notification (OP Code: {op_code}) - تم تجاهلها.")
                continue

            body_slice.load_bits(64)  # تخطي query_id
            jetton_amount = body_slice.load_coins() / 1e6 # USDT decimals = 6
            jetton_sender_wallet = body_slice.load_address().to_str(1, 1, 1) # عنوان محفظة Jetton المرسلة
            recipient_address = body_slice.load_address().to_str(1,1,1) # عنوان المستلم Jetton (يجب أن يكون محفظة البوت)
            logging.info(
                f"💸 معاملة Jetton Transfer Notification tx_hash: {tx_hash_hex}, كمية Jetton: {jetton_amount}, "
                f"مرسل Jetton: {jetton_sender_wallet}, مستلم Jetton: {recipient_address}" # ✅ تسجيل تفاصيل Jetton
            )

            # ✅ التحقق من أن عنوان المستلم هو نفسه عنوان محفظة البوت (اختياري - للتحقق)
            if recipient_address != my_wallet_address:
                logging.warning(f"⚠️ عنوان مستلم Jetton ({recipient_address}) لا يطابق عنوان محفظة البوت المتوقع ({my_wallet_address})! - المعاملة tx_hash: {tx_hash_hex} قد لا تكون صحيحة.")

            if body_slice.load_bit(): # custom_payload bit
                logging.info(f"📦 المعاملة tx_hash: {tx_hash_hex} تحتوي على حمولة مخصصة.") # ✅ تسجيل وجود حمولة مخصصة
                forward_payload = body_slice.load_ref().begin_parse()
                payload_op_code = forward_payload.load_uint(32)
                logging.info(f"⚙️ رمز العملية (OP Code) للحمولة المخصصة للمعاملة tx_hash: {tx_hash_hex} هو {payload_op_code}") # ✅ تسجيل OP Code للحمولة

                if payload_op_code == 0x00000000: # OP Code للتعليق النصي
                    comment = forward_payload.load_string_tail()
                    logging.info(f"💬 التعليق النصي في الحمولة المخصصة للمعاملة tx_hash: {tx_hash_hex} هو: '{comment}'") # ✅ تسجيل التعليق النصي

                    if comment.startswith("orderId:"):
                        order_id_from_payload = comment[len("orderId:"):]
                        logging.info(f"📦 تم استخراج orderId من الحمولة المخصصة للمعاملة tx_hash: {tx_hash_hex}: {order_id_from_payload}") # ✅ تسجيل orderId المستخرج

                        async with current_app.db_pool.acquire() as conn:
                            logging.info(f"🔍 البحث عن دفعة معلقة في قاعدة البيانات لـ userWalletAddress: {sender_wallet_address} و orderId: {order_id_from_payload}") # ✅ تسجيل البحث في قاعدة البيانات
                            pending_payment = await fetch_pending_payment_by_wallet(conn, sender_wallet_address) # البحث عن دفعة معلقة بعنوان المحفظة المرسلة
                            if pending_payment:
                                payment_id_db = pending_payment['payment_id']
                                telegram_id_db = pending_payment['telegram_id']
                                subscription_type_id_db = pending_payment['subscription_type_id']
                                username_db = pending_payment['username']
                                full_name_db = pending_payment['full_name']
                                order_id_db = pending_payment['order_id'] # ✅ استرجاع order_id من قاعدة البيانات

                                logging.info(f"✅ تم العثور على دفعة معلقة في قاعدة البيانات لـ userWalletAddress: {sender_wallet_address}, payment_id: {payment_id_db}, orderId من قاعدة البيانات: {order_id_db}, orderId من الحمولة: {order_id_from_payload}")

                                # ✅ التحقق من تطابق orderId وعنوان المحفظة المرسلة
                                if order_id_db == order_id_from_payload: # ✅ التحقق من تطابق orderId
                                    logging.info(f"✅ تطابق orderId: قاعدة البيانات '{order_id_db}' == الحمولة '{order_id_from_payload}'")

                                    tx_hash = transaction.cell.hash.hex()
                                    updated_payment_data = await update_payment_with_txhash(conn, payment_id_db, tx_hash) # تحديث حالة الدفع وتخزين tx_hash
                                    if updated_payment_data:
                                        logging.info(f"✅ تم تحديث حالة الدفع إلى 'مكتمل' وتخزين tx_hash في قاعدة البيانات لـ payment_id: {payment_id_db}, tx_hash: {tx_hash}")

                                        # استدعاء /api/subscribe لتجديد الاشتراك
                                        async with aiohttp.ClientSession() as session:
                                            headers = {
                                                "Authorization": f"Bearer {WEBHOOK_SECRET_BACKEND}",
                                                "Content-Type": "application/json"
                                            }
                                            subscription_payload = {
                                                "telegram_id": telegram_id_db,
                                                "subscription_type_id": subscription_type_id_db,
                                                "payment_id": payment_id_db, # استخدام payment_id من قاعدة البيانات
                                                "txHash": tx_hash, # تضمين txHash في البيانات المرسلة إلى /api/subscribe
                                                "username": username_db,
                                                "full_name": full_name_db,
                                            }
                                            logging.info(f"📞 استدعاء /api/subscribe لتجديد الاشتراك: {json.dumps(subscription_payload, indent=2)}")
                                            async with session.post(subscribe_api_url, json=subscription_payload, headers=headers) as response:
                                                subscribe_response = await response.json()
                                                if response.status == 200:
                                                    logging.info(f"✅ تم استدعاء /api/subscribe بنجاح! الاستجابة: {subscribe_response}")
                                                else:
                                                    logging.error(f"❌ فشل استدعاء /api/subscribe! الحالة: {response.status}, التفاصيل: {subscribe_response}")
                                    else:
                                        logging.error(f"❌ فشل تحديث حالة الدفع في قاعدة البيانات لـ payment_id: {payment_id_db}")
                                else:
                                    logging.warning(f"⚠️ عدم تطابق orderId: قاعدة البيانات '{order_id_db}', الحمولة '{order_id_from_payload}' - تم تجاهل المعاملة.") # ✅ تسجيل عدم التطابق
                            else:
                                logging.warning(f"⚠️ لم يتم العثور على دفعة معلقة لـ userWalletAddress: {sender_wallet_address} و orderId: {order_id_from_payload} في قاعدة البيانات.")
                    else:
                        logging.warning(f"⚠️ تعليق نصي في الحمولة المخصصة للمعاملة tx_hash: {tx_hash_hex} لا يبدأ بـ 'orderId:' - تم تجاهل المعاملة.")
                else:
                    logging.warning(f"⚠️ حمولة مخصصة غير معروفة (OP Code: {payload_op_code}) للمعاملة tx_hash: {tx_hash_hex} - تم تجاهل المعاملة.")
            else:
                logging.warning(f"⚠️ المعاملة tx_hash: {tx_hash_hex} لا تحتوي على حمولة مخصصة في معاملة Jetton - تم تجاهل المعاملة.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة المعاملات الدورية: {str(e)}", exc_info=True)
    finally:
        logging.info("✅ انتهاء parse_transactions.") # ✅ تسجيل نهاية الدالة

async def periodic_check_payments(): # ✅ دالة الفحص الدوري
    """
    تقوم هذه الدالة بتنفيذ فحص دوري للمعاملات كل فترة زمنية محددة.
    """
    logging.info("⏰ بدء دورة التحقق الدورية من المدفوعات...") # ✅ تسجيل بداية الدورة
    while True:
        provider = None
        logging.info("🔄 بدء دورة parse_transactions الدورية...") # ✅ تسجيل بداية دورة parse_transactions
        try:
            provider = LiteBalancer.from_mainnet_config(1) # أو config أخرى مناسبة
            await provider.start_up()
            await parse_transactions(provider) # استدعاء دالة معالجة المعاملات
        except Exception as e:
            logging.error(f"❌ خطأ في الفحص الدوري للمعاملات: {str(e)}", exc_info=True)
        finally:
            if provider: # ✅ التحقق من أن provider ليس None
                await provider.close_all() # ✅ تبسيط الإغلاق - الاعتماد على الواجهة العامة
        logging.info("✅ انتهاء دورة parse_transactions الدورية. سيتم إعادة التشغيل بعد 60 ثانية.") # ✅ تسجيل نهاية دورة parse_transactions
        await asyncio.sleep(60)  # ✅ انتظار لمدة 60 ثانية (دقيقة واحدة) قبل الفحص الدوري التالي

@payment_confirmation_bp.before_app_serving
async def startup(): # ✅ دالة startup لبدء الفحص الدوري عند تشغيل التطبيق
    """
    دالة يتم استدعاؤها قبل بدء تشغيل التطبيق لبدء مهمة الفحص الدوري للمعاملات.
    """
    logging.info("🚀 بدء مهمة الفحص الدوري للمعاملات في الخلفية...") # ✅ تسجيل بداية مهمة الفحص الدوري
    asyncio.create_task(periodic_check_payments()) # بدء مهمة الفحص الدوري في الخلفية
    logging.info("✅ تم بدء مهمة الفحص الدوري للمعاملات في الخلفية.") # ✅ تسجيل اكتمال بدء المهمة

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    """
    نقطة API لتأكيد استلام الدفع وتسجيل بيانات المستخدم كدفعة معلقة.
    هذه النقطة لا تقوم الآن بمعالجة الدفع أو تجديد الاشتراك بشكل مباشر.
    """
    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!") # ✅ تسجيل استدعاء نقطة النهاية
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}") # ✅ تسجيل بيانات الطلب المستلمة

        webhook_secret_frontend = data.get("webhookSecret")
        if not webhook_secret_frontend or webhook_secret_frontend != WEBHOOK_SECRET_BACKEND:
            logging.warning("❌ طلب غير مصرح به إلى /api/confirm_payment: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403

        user_wallet_address = data.get("userWalletAddress")
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")
        order_id = data.get("orderId") # ✅ استلام orderId من الواجهة الأمامية

        # ... (بقية التحقق من صحة البيانات الأساسية كما هي)

        logging.info(
            f"✅ استلام طلب تأكيد الدفع: userWalletAddress={user_wallet_address}, orderId={order_id}, " # ✅ تضمين orderId في التسجيل
            f"planId={plan_id_str}, telegramId={telegram_id_str}, username={telegram_username}, full_name={full_name}"
        )

        amount = 0

        try:
            plan_id = int(plan_id_str)
            if plan_id == 1:
                subscription_type_id = 1  # Basic plan
            elif plan_id == 2:
                subscription_type_id = 2  # Premium plan
            else:
                subscription_type_id = 1
                logging.warning(f"⚠️ planId غير صالح: {plan_id_str}. تم استخدام الخطة الأساسية افتراضيًا.")
        except ValueError:
            subscription_type_id = 1
            logging.warning(f"⚠️ planId ليس عددًا صحيحًا: {plan_id_str}. تم استخدام الخطة الأساسية افتراضيًا.")

        try:
            telegram_id = int(telegram_id_str)
        except ValueError:
            logging.error(f"❌ telegramId ليس عددًا صحيحًا: {telegram_id_str}. تعذر تسجيل الدفعة.")
            return jsonify({"error": "Invalid telegramId", "details": "telegramId must be an integer."}), 400

        logging.info("💾 جاري تسجيل الدفعة المعلقة في قاعدة البيانات...") # ✅ تسجيل بدء تسجيل الدفعة
        async with current_app.db_pool.acquire() as conn:
            result = await record_payment(
                conn,
                telegram_id,
                user_wallet_address,
                amount,
                subscription_type_id,
                username=telegram_username,
                full_name=full_name,
                order_id=order_id # ✅ تسجيل order_id في قاعدة البيانات
            )

        if result:
            payment_id_db_row = result # الآن result هو قاموس يحتوي على payment_id و payment_date
            payment_id_db = payment_id_db_row['payment_id'] # استخراج payment_id من القاموس
            logging.info(f"✅ تم تسجيل الدفعة المعلقة بنجاح في قاعدة البيانات. payment_id={payment_id_db}, orderId={order_id}") # ✅ تسجيل نجاح تسجيل الدفعة
            logging.info(
                f"💾 تم تسجيل بيانات الدفع والمستخدم كدفعة معلقة: userWalletAddress={user_wallet_address}, orderId={order_id}, " # ✅ تضمين orderId في التسجيل
                f"planId={plan_id_str}, telegramId={telegram_id}, subscription_type_id={subscription_type_id}, payment_id={payment_id_db}, "
                f"username={telegram_username}, full_name={full_name}"
            )
            return jsonify({"message": "Payment confirmation recorded successfully. Waiting for payment processing."}), 200
        else:
            logging.error("❌ فشل تسجيل الدفعة في قاعدة البيانات.")
            return jsonify({"error": "Failed to record payment"}), 500

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500