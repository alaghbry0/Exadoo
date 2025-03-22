from uuid import uuid4
import logging
import asyncio
from quart import Blueprint, request, jsonify, current_app
import json
import os
import aiohttp
from database.db_queries import record_payment, update_payment_with_txhash, fetch_pending_payment_by_orderid
from pytoniq import LiteBalancer, begin_cell, Address
from pytoniq.liteclient.client import LiteServerError
from typing import Optional  # لإضافة تلميحات النوع
from server.redis_manager import redis_manager
from asyncpg.exceptions import UniqueViolationError
from config import DATABASE_CONFIG

# تحميل المتغيرات البيئية

WEBHOOK_SECRET_BACKEND = os.getenv("WEBHOOK_SECRET")
subscribe_api_url = os.getenv("SUBSCRIBE_API_URL")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")  # مفتاح Toncenter

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)


def normalize_address(addr_str: str) -> str:
    """
    دالة مساعدة لتوحيد تنسيق العناوين (لأغراض التسجيل فقط)
    """
    try:
        if addr_str.startswith("0:"):
            addr_str = addr_str[2:]
        addr = Address(addr_str)
        return addr.to_str(is_user_friendly=True, is_bounceable=False, is_url_safe=True).strip()
    except Exception as e:
        logging.warning(f"❌ فشل تطبيع العنوان {addr_str}: {str(e)}")
        return addr_str.strip()


# دالة تحويل القيمة إلى الوحدة المطلوبة
def convert_amount(raw_value: int, decimals: int = 9) -> float:
    return raw_value / (10 ** decimals)

# دالة لاسترجاع سعر الاشتراك من جدول subscription_plans
async def get_subscription_price(conn, subscription_plan_id: int) -> float:
    query = "SELECT price FROM subscription_plans WHERE id = $1"
    row = await conn.fetchrow(query, subscription_plan_id)
    return float(row['price']) if row and row['price'] is not None else 0.0

async def retry_get_transactions(provider: LiteBalancer, address: str, count: int = 10,
                                 retries: int = 3, initial_delay: int = 5, backoff: int = 2):
    """
    تحاول هذه الدالة جلب المعاملات مع إعادة المحاولة عند ظهور أخطاء معينة مثل -400 أو "have no alive peers".
    """
    delay = initial_delay
    for attempt in range(1, retries + 1):
        try:
            transactions = await provider.get_transactions(address=address, count=count)
            return transactions
        except LiteServerError as e:
            if e.code == -400:
                logging.warning("تحذير: Liteserver لم يعثر على المعاملة. محاولة رقم %d/%d بعد %d ثانية...", attempt, retries, delay)
            else:
                raise e
        except Exception as e:
            if "have no alive peers" in str(e):
                logging.warning("تحذير: لا يوجد نظائر حية. محاولة رقم %d/%d بعد %d ثانية...", attempt, retries, delay)
            else:
                raise e
        await asyncio.sleep(delay)
        delay *= backoff
    raise Exception("فشل الحصول على المعاملات بعد {} محاولات".format(retries))


async def parse_transactions(provider: LiteBalancer):
    """
    تقوم هذه الدالة بجلب آخر المعاملات من محفظة البوت وتحليلها.
    """
    logging.info("🔄 بدء parse_transactions...")

    # الحصول على عنوان المحفظة من قاعدة البيانات عبر الدالة الجديدة
    my_wallet_address: Optional[str] = await get_bot_wallet_address()
    if not my_wallet_address:
        logging.error("❌ لم يتم تعريف عنوان محفظة البوت في قاعدة البيانات!")
        return

    # استخدام العنوان المُطَبعة عند استعلام المعاملات
    normalized_bot_address = normalize_address(my_wallet_address)
    logging.info(f"🔍 جلب آخر المعاملات من محفظة البوت: {normalized_bot_address}")

    try:
        try:
            transactions = await provider.get_transactions(address=normalized_bot_address, count=10)
        except LiteServerError as e:
            if e.code == -400:
                logging.warning("تحذير: Liteserver لم يعثر على المعاملة بالوقت المنطقي المحدد. قد يكون خطأ مؤقتاً.")
                return
            else:
                raise e

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

            # استخراج عنوان المُرسل لأغراض التسجيل فقط
            sender_wallet_address = transaction.in_msg.info.src.to_str(1, 1, 1)
            normalized_sender = normalize_address(sender_wallet_address)
            value = transaction.in_msg.info.value_coins
            if value != 0:
                value = convert_amount(value, 9)
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

            # تحويل قيمة Jetton باستخدام دالة التحويل
            jetton_amount = convert_amount(body_slice.load_coins(), 9)
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
                        # استخراج القيمة مباشرة بدون التحقق من بادئة "orderId:"
                        order_id_from_payload = comment.strip()
                        logging.info(f"📦 تم استخراج orderId: '{order_id_from_payload}' من tx_hash: {tx_hash_hex}")
                    except Exception as e:
                        logging.error(f"❌ خطأ أثناء قراءة التعليق في tx_hash: {tx_hash_hex}: {str(e)}")
                        continue
                else:
                    logging.warning(f"⚠️ معاملة tx_hash: {tx_hash_hex} تحتوي على OP Code غير معروف في forward payload: {forward_payload_op_code}")
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
                if db_order_id != order_id_from_payload:
                    logging.warning(f"⚠️ عدم تطابق orderId: DB '{db_order_id}' vs payload '{order_id_from_payload}' - تجاهل tx_hash: {tx_hash_hex}")
                    continue

                # استرجاع سعر الاشتراك من جدول subscription_plans باستخدام subscription_plan_id
                subscription_plan_id = pending_payment['subscription_plan_id']
                expected_subscription_price = await get_subscription_price(conn, subscription_plan_id)
                tolerance = 0.30  # الفارق المسموح به
                logging.info(f"🔍 سعر الاشتراك: {expected_subscription_price}, tolerance: {tolerance}")

                # مقارنة مبلغ الدفع مع سعر الاشتراك
                difference = expected_subscription_price - jetton_amount
                if difference < 0:
                    # دفعة زائدة
                    await redis_manager.publish_event(
                        f"payment_{pending_payment['payment_token']}",
                        {
                            'status': 'success',
                            'message': 'لقد قمت بإرسال دفعة زائدة. يرجى التواصل مع الدعم لاسترداد الفرق.'
                        }
                    )
                    await asyncio.sleep(3)
                elif difference > tolerance:
                    # دفعة ناقصة خارج الفارق المسموح
                    await redis_manager.publish_event(
                        f"payment_{pending_payment['payment_token']}",
                        {
                            'status': 'failed',
                            'message': 'فشل تجديد الاشتراك لأن الدفعة التي أرسلتها أقل من المبلغ المطلوب، الرجاء التواصل مع الدعم.'
                        }
                    )
                    continue
                else:
                    # دفعة ناقصة ضمن الفارق المسموح
                    await redis_manager.publish_event(
                        f"payment_{pending_payment['payment_token']}",
                        {
                            'status': 'success',
                            'message': 'يبدوا انه لم يتم احتساب رسوم الشبكة في الدفعه, هذه المره سنقوم بتجديد اشتراكك, لذا نرجوا ان يتم تضمينها في المره القادمه. '
                        }
                    )

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
                            "telegram_id": int(pending_payment['telegram_id']),
                            "subscription_plan_id": pending_payment['subscription_plan_id'],
                            "payment_id": tx_hash,
                            "payment_token": pending_payment['payment_token'],
                            "username": str(pending_payment['username']),
                            "full_name": str(pending_payment['full_name']),
                        }
                        logging.info(f"📞 استدعاء /api/subscribe لتجديد الاشتراك بالبيانات: {json.dumps(subscription_payload, indent=2)}")
                        try:
                            async with session.post(subscribe_api_url, json=subscription_payload, headers=headers) as response:
                                if response.status == 200:
                                    subscribe_data = await response.json()
                                    logging.info(f"✅ تم استدعاء /api/subscribe بنجاح! الاستجابة: {subscribe_data}")
                                else:
                                    error_details = await response.text()
                                    logging.error(f"❌ فشل استدعاء /api/subscribe! الحالة: {response.status}, التفاصيل: {error_details}")
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
    """
    تقوم هذه الدالة بالتحقق الدوري من المعاملات باستخدام LiteBalancer،
    وتستخدم الاتصال المشترك في دوال تحليل المعاملات.
    """
    while True:
        provider = None
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
        logging.info("✅ انتهاء دورة parse_transactions الدورية. سيتم إعادة التشغيل بعد 30 ثانية.")
        await asyncio.sleep(30)


@payment_confirmation_bp.before_app_serving
async def startup():
    logging.info("🚀 بدء تهيئة وحدة تأكيد المدفوعات...")
    timeout = 120  # ⏳ 120 ثانية
    start_time = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout:
            logging.error(f"""
            ❌ فشل حرج بعد {timeout} ثانية:
            - db_pool موجود؟ {hasattr(current_app, 'db_pool')}
            - حالة Redis: {await redis_manager.is_connected()}
            """)
            raise RuntimeError("فشل التهيئة")

        if hasattr(current_app, 'db_pool') and current_app.db_pool is not None:
            try:
                async with current_app.db_pool.acquire() as conn:
                    await conn.execute("SELECT 1")
                logging.info("✅ اتصال قاعدة البيانات فعّال")
                break
            except Exception as e:
                logging.warning(f"⚠️ فشل التحقق من اتصال قاعدة البيانات: {str(e)}")
                await asyncio.sleep(5)
        else:
            logging.info(f"⏳ انتظار db_pool... ({elapsed:.1f}/{timeout} ثانية)")
            await asyncio.sleep(5)

    logging.info("🚦 بدء المهام الخلفية...")
    asyncio.create_task(periodic_check_payments())



async def handle_failed_transaction(tx_hash: str, retries: int = 3):
    for attempt in range(retries):
        try:
            # محاولة معالجة المعاملة مرة أخرى (يفترض تعريف process_transaction في مكان آخر)
            await process_transaction(tx_hash)
            break
        except Exception as e:
            logging.warning(f"⚠️ محاولة {attempt+1} فشلت: {str(e)}")
            await asyncio.sleep(5 * (attempt + 1))

@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}")

        # التحقق من مفتاح الويب هوك المرسل من الواجهة الأمامية
        webhook_secret_frontend = data.get("webhookSecret")
        if not webhook_secret_frontend or webhook_secret_frontend != os.getenv("WEBHOOK_SECRET"):
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
            subscription_plan_id = int(plan_id_str)
        except (ValueError, TypeError):
            subscription_plan_id = 1
            logging.warning(f"⚠️ planId ليس عددًا صحيحًا: {plan_id_str}. تم استخدام الخطة الأساسية افتراضيًا.")

        try:
            telegram_id = int(telegram_id_str)
        except (ValueError, TypeError):
            logging.error(f"❌ telegramId ليس عددًا صحيحًا: {telegram_id_str}. تعذر تسجيل الدفعة.")
            return jsonify({"error": "Invalid telegramId", "details": "telegramId must be an integer."}), 400

        # إنشاء payment_token فريد
        payment_token = str(uuid4())

        logging.info("💾 جاري تسجيل الدفعة المعلقة في قاعدة البيانات...")
        result = None
        max_attempts = 3  # تحديد الحد الأقصى لمحاولات إعادة التسجيل
        attempt = 0

        async with current_app.db_pool.acquire() as conn:
            # حذف السجلات القديمة من جدول telegram_payments (أكثر من ساعتين)

            while attempt < max_attempts:
                try:
                    result = await record_payment(
                        conn=conn,
                        telegram_id=telegram_id,
                        user_wallet_address=user_wallet_address,
                        amount=amount,
                        subscription_plan_id=subscription_plan_id,
                        username=telegram_username,
                        full_name=full_name,
                        order_id=order_id,
                        payment_token=payment_token
                    )
                    break
                except UniqueViolationError:
                    attempt += 1
                    logging.warning("⚠️ تكرار payment_token، إعادة المحاولة...")
                    payment_token = str(uuid4())

            if result is None:
                logging.error("❌ فشل تسجيل الدفعة بعد محاولات متعددة بسبب تضارب payment_token.")
                return jsonify({"error": "Failed to record payment after retries"}), 500

            # تسجيل نفس البيانات في جدول telegram_payments
            try:
                await conn.execute('''
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
                logging.info(f"✅ تم تسجيل البيانات في جدول telegram_payments: {payment_token}")
            except UniqueViolationError as uve:
                logging.warning(f"⚠️ تكرار في telegram_payments لرمز الدفع {payment_token}: {uve}")
            except Exception as e:
                logging.error(f"❌ خطأ أثناء تسجيل البيانات في جدول telegram_payments: {str(e)}")

        logging.info(
            f"✅ تم تسجيل الدفعة المعلقة بنجاح في قاعدة البيانات. payment_token={payment_token}, orderId={order_id}"
        )
        return jsonify({
            "success": True,
            "payment_token": payment_token,
            "order_id": order_id
        }), 200

    except Exception as e:
        logging.error(f"❌ خطأ في /api/confirm_payment: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# تغيير قيمة timestamp إلى float لتفادي تحذيرات النوع
_wallet_cache = {
    "address": None,
    "timestamp": 0.0
}
WALLET_CACHE_TTL = 60  # زمن التخزين المؤقت بالثواني (مثلاً 60 ثانية)


async def get_bot_wallet_address() -> Optional[str]:
    global _wallet_cache
    now = asyncio.get_event_loop().time()
    if not hasattr(current_app, 'db_pool') or current_app.db_pool is None:
        logging.error("❌ db_pool غير مهيأ!")
        return None

    # التحقق من صلاحية الكاش أو انتهاء مدة التخزين المؤقت
    if _wallet_cache["address"] is None or now - _wallet_cache["timestamp"] > WALLET_CACHE_TTL:
        async with current_app.db_pool.acquire() as connection:
            wallet = await connection.fetchrow("SELECT wallet_address FROM wallet ORDER BY id DESC LIMIT 1")
            if not wallet:
                logging.error("❌ لا يوجد عنوان محفظة مسجل في قاعدة البيانات!")
                return None
            _wallet_cache["address"] = wallet["wallet_address"]
            _wallet_cache["timestamp"] = now
    return _wallet_cache["address"]
