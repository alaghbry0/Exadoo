import uuid
import logging
import asyncio
from quart import Blueprint, request, jsonify, current_app
import json
import os
import aiohttp
from database.db_queries import record_payment, update_payment_with_txhash, fetch_pending_payment_by_orderid
from pytoniq import LiteBalancer, begin_cell, Address

# تحميل المتغيرات البيئية
WEBHOOK_SECRET_BACKEND = os.getenv("WEBHOOK_SECRET")
subscribe_api_url = os.getenv("SUBSCRIBE_API_URL")
BOT_WALLET_ADDRESS = os.getenv("BOT_WALLET_ADDRESS")  # عنوان محفظة البوت (Non‑bounceable)
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")      # مفتاح Toncenter

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

# دالة مساعدة لتوحيد تنسيق العناوين (لأغراض التسجيل فقط)
def normalize_address(addr_str: str) -> str:
    try:
        if addr_str.startswith("0:"):
            addr_str = addr_str[2:]
        addr = Address(addr_str)
        return addr.to_str(is_user_friendly=True, is_bounceable=False, is_url_safe=True).lower().strip()
    except Exception as e:
        logging.warning(f"❌ فشل تطبيع العنوان {addr_str}: {str(e)}")
        return addr_str.lower().strip()

async def parse_transactions(provider: LiteBalancer):
    """
    تقوم هذه الدالة بجلب آخر المعاملات من محفظة البوت،
    وتحليل payload تحويل Jetton وفقًا لمعيار TEP‑74 واستخراج orderId،
    ثم مقارنة بيانات الدفع مع الدفعات المعلقة في قاعدة البيانات باستخدام orderId فقط.
    """
    logging.info("🔄 بدء parse_transactions...")
    my_wallet_address = BOT_WALLET_ADDRESS
    if not my_wallet_address:
        logging.error("❌ لم يتم تعريف BOT_WALLET_ADDRESS في متغيرات البيئة!")
        return

    normalized_bot_address = normalize_address(my_wallet_address)
    logging.info(f"🔍 جلب آخر المعاملات من محفظة البوت: {normalized_bot_address}")
    try:
        transactions = await provider.get_transactions(address=my_wallet_address, count=10)
        logging.info(f"✅ تم جلب {len(transactions)} معاملة.")

        for transaction in transactions:
            tx_hash_hex = transaction.cell.hash.hex()
            logging.info(f"🔄 فحص المعاملة tx_hash: {tx_hash_hex}")

            if not transaction.in_msg.is_internal:
                logging.info(f"➡️ معاملة tx_hash: {tx_hash_hex} ليست داخلية - تم تجاهلها.")
                continue

            dest_address = normalize_address(transaction.in_msg.info.dest.to_str(1, 1, 1))
            if dest_address != normalized_bot_address:
                logging.info(f"➡️ معاملة tx_hash: {tx_hash_hex} ليست موجهة إلى محفظة البوت (dest: {dest_address} vs expected: {normalized_bot_address}) - تم تجاهلها.")
                continue

            # نستخرج عنوان المُرسل لأغراض التسجيل فقط
            sender_wallet_address = transaction.in_msg.info.src.to_str(1, 1, 1)
            normalized_sender = normalize_address(sender_wallet_address)
            value = transaction.in_msg.info.value_coins
            if value != 0:
                value = value / 1e9
            logging.info(f"💰 معاملة tx_hash: {tx_hash_hex} من {normalized_sender} بقيمة {value} TON.")

            if len(transaction.in_msg.body.bits) < 32:
                logging.info(f"➡️ معاملة tx_hash: {tx_hash_hex} تبدو كتحويل TON وليس Jetton - تم تجاهلها.")
                continue

            body_slice = transaction.in_msg.body.begin_parse()
            op_code = body_slice.load_uint(32)
            logging.info(f"📌 OP Code الأساسي: {hex(op_code)}")
            if op_code not in (0xf8a7ea5, 0x7362d09c):
                logging.info(f"➡️ معاملة tx_hash: {tx_hash_hex} OP Code ({hex(op_code)}) غير متوافق مع تحويل Jetton - تم تجاهلها.")
                continue

            body_slice.load_bits(64)  # تخطي query_id

            jetton_amount = body_slice.load_coins() / 1e9
            logging.info(f"💸 قيمة Jetton: {jetton_amount}")
            jetton_sender = body_slice.load_address().to_str(1, 1, 1)
            normalized_jetton_sender = normalize_address(jetton_sender)
            logging.info(f"📤 عنوان المرسل من payload: {normalized_jetton_sender}")

            try:
                remaining_bits = len(body_slice.bits)
                logging.info(f"📌 عدد البتات المتبقية قبل forward payload: {remaining_bits}")
                forward_payload = body_slice.load_ref().begin_parse() if body_slice.load_bit() else body_slice
                logging.info("✅ تم استخراج forward payload.")
            except Exception as e:
                logging.error(f"❌ خطأ أثناء استخراج forward payload في tx_hash: {tx_hash_hex}: {str(e)}")
                continue

            logging.info(f"📌 عدد البتات في forward payload: {len(forward_payload.bits)}")

            # الحصول على expected_jetton_wallet لأغراض التسجيل فقط
            try:
                jetton_master = (await provider.run_get_method(
                    address=sender_wallet_address, method="get_wallet_data", stack=[]
                ))[2].load_address()
                expected_jetton_wallet = (await provider.run_get_method(
                    address=jetton_master,
                    method="get_wallet_address",
                    stack=[begin_cell().store_address(my_wallet_address).end_cell().begin_parse()],
                ))[0].load_address().to_str(is_user_friendly=True, is_bounceable=False, is_url_safe=True)
                logging.info(f"📌 عنوان الجيتون المستخرج من العقد (للتسجيل فقط): {expected_jetton_wallet}")
            except Exception as e:
                logging.warning(f"⚠️ تجاوز التحقق من عنوان الجيتون بسبب الخطأ: {str(e)}")
                expected_jetton_wallet = normalized_jetton_sender

            normalized_expected = normalize_address(expected_jetton_wallet)
            logging.info(f"🔍 (للتسجيل) مقارنة العناوين: payload={normalized_jetton_sender} vs expected={normalized_expected}")
            logging.info("✅ سيتم استخدام orderId للمطابقة مع قاعدة البيانات.")

            # استخراج forward payload للتعليق (orderId)
            order_id_from_payload = None
            if len(forward_payload.bits) < 32:
                logging.info(f"💸 معاملة tx_hash: {tx_hash_hex} بدون forward payload (تعليق).")
            else:
                forward_payload_op_code = forward_payload.load_uint(32)
                logging.info(f"📌 OP Code داخل forward payload: {forward_payload_op_code}")
                if forward_payload_op_code == 0:
                    try:
                        comment = forward_payload.load_snake_string()
                        logging.info(f"📌 التعليق الكامل المستخرج: {comment}")
                    except Exception as e:
                        logging.error(f"❌ خطأ أثناء قراءة التعليق في tx_hash: {tx_hash_hex}: {str(e)}")
                        continue
                    if comment.startswith("orderId:"):
                        order_id_from_payload = comment[len("orderId:"):].strip()
                        logging.info(f"📦 تم استخراج orderId: '{order_id_from_payload}' من tx_hash: {tx_hash_hex}")
                    else:
                        logging.warning(f"⚠️ التعليق في tx_hash: {tx_hash_hex} لا يبدأ بـ 'orderId:' - تجاهل المعاملة.")
                        continue
                else:
                    logging.warning(f"⚠️ معاملة tx_hash: {tx_hash_hex} تحتوي على OP Code غير معروف في forward payload: {forward_payload_op_code}")
                    continue

            if not order_id_from_payload:
                logging.warning(f"⚠️ لم يتم استخراج orderId من tx_hash: {tx_hash_hex} - تجاهل المعاملة.")
                continue

            logging.info(f"✅ orderId المستخرج: {order_id_from_payload}")

            # المطابقة مع قاعدة البيانات باستخدام orderId فقط
            async with current_app.db_pool.acquire() as conn:
                logging.info(f"🔍 البحث عن دفعة معلقة باستخدام orderId: {order_id_from_payload}")
                pending_payment = await fetch_pending_payment_by_orderid(conn, order_id_from_payload)
                if not pending_payment:
                    logging.warning(f"⚠️ لم يتم العثور على سجل دفع معلق لـ orderId: {order_id_from_payload}")
                    continue

                db_order_id = pending_payment['order_id'].strip()
                db_amount = float(pending_payment.get('amount', 0))
                logging.info(f"🔍 الدفعة المعلقة الموجودة: order_id: '{db_order_id}', amount: {db_amount}")
                logging.info(f"🔍 مقارنة: payload orderId: '{order_id_from_payload}' vs DB orderId: '{db_order_id}'")
                if db_order_id != order_id_from_payload:
                    logging.warning(f"⚠️ عدم تطابق orderId: DB '{db_order_id}' vs payload '{order_id_from_payload}' - تجاهل tx_hash: {tx_hash_hex}")
                    continue
                if abs(db_amount - jetton_amount) > 1e-9:
                    logging.warning(f"⚠️ عدم تطابق مبلغ الدفع: DB amount {db_amount} vs jetton_amount {jetton_amount} في tx_hash: {tx_hash_hex} - تجاهل المعاملة.")
                    continue

                logging.info(f"✅ تطابق بيانات الدفع. متابعة التحديث لـ payment_id: {pending_payment['payment_id']}")
                tx_hash = tx_hash_hex
                updated_payment_data = await update_payment_with_txhash(conn, pending_payment['payment_id'], tx_hash)
                if updated_payment_data:
                    logging.info(f"✅ تم تحديث سجل الدفع إلى 'مكتمل' لـ payment_id: {pending_payment['payment_id']}، tx_hash: {tx_hash}")
                    async with aiohttp.ClientSession() as session:
                        headers = {
                            "Authorization": f"Bearer {WEBHOOK_SECRET_BACKEND}",
                            "Content-Type": "application/json"
                        }
                        subscription_payload = {
                            "telegram_id": pending_payment['telegram_id'],
                            "subscription_type_id": pending_payment['subscription_type_id'],
                            "payment_id": tx_hash,  # استخدام tx_hash كـ payment_id
                            "username": pending_payment['username'],
                            "full_name": pending_payment['full_name'],
                        }
                        logging.info(f"📞 استدعاء /api/subscribe لتجديد الاشتراك بالبيانات: {json.dumps(subscription_payload, indent=2)}")
                        try:
                            async with session.post(subscribe_api_url, json=subscription_payload, headers=headers) as response:
                                subscribe_response = await response.json()
                                if response.status == 200:
                                    logging.info(f"✅ تم استدعاء /api/subscribe بنجاح! الاستجابة: {subscribe_response}")
                                else:
                                    logging.error(f"❌ فشل استدعاء /api/subscribe! الحالة: {response.status}, التفاصيل: {subscribe_response}")
                        except Exception as e:
                            logging.error(f"❌ استثناء أثناء استدعاء /api/subscribe: {str(e)}")
                else:
                    logging.error(f"❌ فشل تحديث حالة الدفع في قاعدة البيانات لـ payment_id: {pending_payment['payment_id']}")
            logging.info(f"📝 Transaction processed: tx_hash: {tx_hash_hex}, lt: {transaction.lt}")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة المعاملات الدورية: {str(e)}", exc_info=True)
    finally:
        logging.info("✅ انتهاء parse_transactions.")

async def periodic_check_payments():
    logging.info("⏰ بدء دورة التحقق الدورية من المدفوعات...")
    while True:
        provider = None
        logging.info("🔄 بدء دورة parse_transactions الدورية...")
        try:
            provider = LiteBalancer.from_mainnet_config(1)
            await provider.start_up()
            await parse_transactions(provider)
        except Exception as e:
            logging.error(f"❌ خطأ في الفحص الدوري للمعاملات: {str(e)}", exc_info=True)
        finally:
            if provider:
                try:
                    await provider.close_all()
                except AttributeError as e:
                    logging.warning(f"⚠️ أثناء إغلاق provider: {e}")
        logging.info("✅ انتهاء دورة parse_transactions الدورية. سيتم إعادة التشغيل بعد 60 ثانية.")
        await asyncio.sleep(20)

@payment_confirmation_bp.before_app_serving
async def startup():
    logging.info("🚀 بدء مهمة الفحص الدوري للمعاملات في الخلفية...")
    asyncio.create_task(periodic_check_payments())
    logging.info("✅ تم بدء مهمة الفحص الدوري للمعاملات في الخلفية.")

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}")

        webhook_secret_frontend = data.get("webhookSecret")
        if not webhook_secret_frontend or webhook_secret_frontend != WEBHOOK_SECRET_BACKEND:
            logging.warning("❌ طلب غير مصرح به إلى /api/confirm_payment: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403

        user_wallet_address = data.get("userWalletAddress")
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")
        order_id = data.get("orderId")
        amount_str = data.get("amount", "0")
        try:
            amount = float(amount_str)
        except ValueError:
            amount = 0.0
            logging.warning(f"⚠️ قيمة amount غير صالحة: {amount_str}. سيتم تعيينها إلى 0.")

        logging.info(
            f"✅ استلام طلب تأكيد الدفع: userWalletAddress={user_wallet_address}, orderId={order_id}, "
            f"planId={plan_id_str}, telegramId={telegram_id_str}, username={telegram_username}, full_name={full_name}, amount={amount}"
        )

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

        logging.info("💾 جاري تسجيل الدفعة المعلقة في قاعدة البيانات...")
        async with current_app.db_pool.acquire() as conn:
            result = await record_payment(
                conn,
                telegram_id,
                user_wallet_address,
                amount,
                subscription_type_id,
                username=telegram_username,
                full_name=full_name,
                order_id=order_id
            )

        if result:
            payment_id_db_row = result
            payment_id_db = payment_id_db_row['payment_id']
            logging.info(f"✅ تم تسجيل الدفعة المعلقة بنجاح في قاعدة البيانات. payment_id={payment_id_db}, orderId={order_id}")
            logging.info(
                f"💾 تم تسجيل بيانات الدفع والمستخدم كدفعة معلقة: userWalletAddress={user_wallet_address}, orderId={order_id}, "
                f"planId={plan_id_str}, telegramId={telegram_id}, subscription_type_id={subscription_type_id}, payment_id={payment_id_db}, "
                f"username={telegram_username}, full_name={full_name}, amount={amount}"
            )
            return jsonify({"message": "Payment confirmation recorded successfully. Waiting for payment processing."}), 200
        else:
            logging.error("❌ فشل تسجيل الدفعة في قاعدة البيانات.")
            return jsonify({"error": "Failed to record payment"}), 500

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
