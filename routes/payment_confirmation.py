from uuid import uuid4
import logging
import asyncio
from quart import Blueprint, request, jsonify, current_app
import json
import os
from decimal import Decimal, ROUND_DOWN, getcontext
import aiohttp
from database.db_queries import record_payment, update_payment_with_txhash, fetch_pending_payment_by_payment_token, \
    record_incoming_transaction, add_user
from pytoniq import LiteBalancer, begin_cell, Address
from pytoniq.liteclient.client import LiteServerError
from typing import Optional  # لإضافة تلميحات النوع

from asyncpg.exceptions import UniqueViolationError
from config import DATABASE_CONFIG
from datetime import datetime
from routes.ws_routes import broadcast_notification

# نفترض أنك قد أنشأت وحدة خاصة بالإشعارات تحتوي على الدالة create_notification
from utils.notifications import create_notification

# تحميل المتغيرات البيئية

WEBHOOK_SECRET_BACKEND = os.getenv("WEBHOOK_SECRET")
subscribe_api_url = os.getenv("SUBSCRIBE_API_URL")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")  # مفتاح Toncenter

payment_confirmation_bp = Blueprint("payment_confirmation", __name__)

# ضبط دقة الأرقام العشرية للتعامل المالي
getcontext().prec = 30

# ضبط مستوى التسجيل (logging) ليكون أكثر تفصيلاً أثناء التطوير
#logging.basicConfig(
    #level=logging.WARNING,
#format='%(asctime)s - %(levelname)s - %(message)s'
#)


# --- دوال مساعدة ---

async def get_subscription_price(conn, subscription_plan_id: int) -> Decimal:
    query = "SELECT price FROM subscription_plans WHERE id = $1"
    row = await conn.fetchrow(query, subscription_plan_id)
    return Decimal(row['price']) if row and row['price'] is not None else Decimal('0.0')


def normalize_address(addr_str: str) -> Optional[str]:
    """
    دالة مساعدة لتوحيد تنسيق عناوين TON.
    ترجع العنوان بصيغة non-bounceable أو None إذا كان غير صالح.
    """
    try:
        # لا حاجة لإزالة "0:" لأن مكتبة pytoniq تتعامل معها
        addr = Address(addr_str)
        return addr.to_str(is_user_friendly=True, is_bounceable=False, is_url_safe=True)
    except Exception:
        logging.warning(f"❌ فشل في تطبيع العنوان: {addr_str}")
        return None


def convert_amount(raw_value: int, decimals: int) -> Decimal:
    """
    تحويل القيمة الأولية (بالوحدات الصغرى) إلى Decimal بناءً على عدد الخانات العشرية.
    """
    if raw_value is None:
        return Decimal('0')
    return Decimal(raw_value) / (10 ** decimals)


async def call_subscription_api(session, payment_data: dict):
    """
    دالة مخصصة لاستدعاء API الخاص بتجديد الاشتراك.
    تستخدم قاموسًا واحدًا يحتوي على بيانات الدفع الكاملة.
    """
    headers = {
        "Authorization": f"Bearer {WEBHOOK_SECRET_BACKEND}",
        "Content-Type": "application/json"
    }
    # استخراج البيانات من القاموس مباشرة
    payload = {
        "telegram_id": payment_data.get('telegram_id'),
        "subscription_plan_id": payment_data.get('subscription_plan_id'),
        "payment_id": payment_data.get('tx_hash'), # سيحتوي الآن على القيمة الصحيحة
        "payment_token": payment_data.get('payment_token'),
        "username": str(payment_data.get('username')),
        "full_name": str(payment_data.get('full_name')),
    }
    # التحقق من أن الحقول الأساسية ليست فارغة قبل الإرسال
    if not all([payload["telegram_id"], payload["subscription_plan_id"], payload["payment_id"]]):
        logging.error(f"❌ بيانات غير مكتملة قبل استدعاء API التجديد: {payload}")
        return

    logging.info(f"📞 استدعاء API التجديد بالبيانات: {json.dumps(payload, indent=2)}")
    try:
        async with session.post(subscribe_api_url, json=payload, headers=headers) as response:
            if response.status == 200:
                response_data = await response.json()
                logging.info(f"✅ تم استدعاء API التجديد بنجاح! الاستجابة: {response_data}")
            else:
                error_details = await response.text()
                logging.error(f"❌ فشل استدعاء API التجديد! الحالة: {response.status}, التفاصيل: {error_details}")
    except Exception as e:
        logging.error(f"❌ استثناء أثناء استدعاء API التجديد: {e}", exc_info=True)


# --- 🟢 إضافة جديدة: دالة الحصول على المعاملات مع نظام إعادة المحاولة ---
async def get_transactions_with_retry(
    provider: LiteBalancer,
    address: str,
    count: int = 10,
    retries: int = 3,
    backoff_factor: float = 2.0
) -> list:
    """
    Tries to get transactions from a LiteBalancer with an exponential backoff retry mechanism.
    """
    for attempt in range(retries):
        try:
            return await provider.get_transactions(address=address, count=count)
        except Exception as e:
            if attempt < retries - 1:
                sleep_time = backoff_factor ** attempt
                logging.warning(
                    f"⚠️ Attempt {attempt + 1}/{retries} failed to get transactions. "
                    f"Error: {e}. Retrying in {sleep_time:.2f} seconds..."
                )
                await asyncio.sleep(sleep_time)
            else:
                logging.error(f"❌ All {retries} attempts failed. Could not get transactions.")
                raise  # Re-raise the last exception if all retries fail
    return [] # In case the loop finishes without returning or raising, return an empty list.

# --- الدالة الرئيسية لمعالجة المعاملات (تم تحديثها بالكامل) ---

async def parse_transactions(provider: LiteBalancer):
    """
    Parses transactions after fetching them reliably using a retry mechanism.
    """
    logging.info("🔄 Starting transaction parsing cycle...")
    my_wallet_address: Optional[str] = await get_bot_wallet_address()
    if not my_wallet_address:
        logging.error("❌ Bot wallet address not defined in the database!")
        return

    normalized_bot_address = normalize_address(my_wallet_address)
    logging.info(f"🔍 Fetching latest transactions for bot wallet: {normalized_bot_address}")

    try:
        # استخدام الدالة الجديدة التي تحتوي على نظام إعادة المحاولة
        transactions = await get_transactions_with_retry(
            provider=provider,
            address=normalized_bot_address
        )
    except Exception as e:
        logging.error(f"❌ خطأ فادح أثناء جلب المعاملات: {e}", exc_info=True)
        return

    if not transactions:
        logging.info("ℹ️ No new transactions found.")
        return

    logging.info(f"✅ Fetched {len(transactions)} transactions.")

    # --- التحسين 1: استخدام اتصال واحد (DRY) ---
    # نفتح اتصالاً واحداً ونستخدمه لكل المعاملات في هذه الدورة
    async with current_app.db_pool.acquire() as conn:
        for transaction in transactions:
            tx_hash_hex = transaction.cell.hash.hex()
            logging.info(f"--- بدء معالجة المعاملة: {tx_hash_hex} ---")

            # ** تعديل مطلوب 2: إضافة تحويل قيمة TON **
            value_coins = transaction.in_msg.info.value_coins
            ton_value = convert_amount(value_coins, 9) if value_coins else Decimal('0')
            if ton_value > 0:
                logging.info(f"💰 قيمة TON المستلمة: {ton_value} TON.")

            # =================================================================
            # الخطوة 1: استخراج البيانات الخام وتسجيل كل معاملة واردة
            # =================================================================
            payment_token_from_payload = None  # سيتم تعبئته لاحقاً
            try:
                # فلترة أولية للمعاملات غير المرغوب فيها
                if not transaction.in_msg.is_internal or \
                        normalize_address(transaction.in_msg.info.dest.to_str(1, 1, 1)) != normalized_bot_address:
                    logging.info(f"➡️ معاملة {tx_hash_hex} ليست داخلية أو ليست موجهة لنا. تم تجاهلها.")
                    continue

                body_slice = transaction.in_msg.body.begin_parse()
                op_code = body_slice.load_uint(32)

                if op_code not in (0xf8a7ea5, 0x7362d09c):  # Jetton transfer/notification
                    logging.info(f"➡️ معاملة {tx_hash_hex} ليست تحويل Jetton. تم تجاهلها.")
                    continue

                body_slice.load_bits(64)  # تخطي query_id
                jetton_amount_raw = body_slice.load_coins()
                jetton_sender_raw = body_slice.load_address().to_str(1, 1, 1)

                # ** تعديل مطلوب 3: تحسين استخراج forward_payload **
                if body_slice.load_bit():  # إذا كان هناك forward_payload في مرجع (ref)
                    forward_payload = body_slice.load_ref().begin_parse()
                else:  # أو إذا كان في الجسم مباشرة
                    forward_payload = body_slice

                # الآن تحقق من وجود التعليق في الحمولة النهائية
                if len(forward_payload.bits) >= 32 and forward_payload.load_uint(32) == 0:
                    comment = forward_payload.load_snake_string()
                    # --- الإصلاح الضروري: تنظيف التعليق ---
                    payment_token_from_payload = comment.replace('\x00', '').strip()

                # تحويل البيانات وتطبيعها
                jetton_amount = convert_amount(jetton_amount_raw, 6)  # 6 أصفار لـ USDT
                normalized_jetton_sender = normalize_address(jetton_sender_raw)

                # تسجيل المعاملة في incoming_transactions
                await record_incoming_transaction(
                    conn=conn,
                    txhash=tx_hash_hex,
                    sender=normalized_jetton_sender,
                    amount=jetton_amount,
                    payment_token=payment_token_from_payload
                )
                logging.info(f"✅ تم تسجيل المعاملة {tx_hash_hex} في incoming_transactions بنجاح.")

            except UniqueViolationError:
                logging.info(f"ℹ️ المعاملة {tx_hash_hex} مسجلة بالفعل. سيتم التحقق إذا كانت تحتاج لمعالجة دفع.")
            except Exception as e:
                logging.error(f"❌ فشل في تحليل وتسجيل المعاملة {tx_hash_hex}: {e}", exc_info=True)
                continue  # انتقل إلى المعاملة التالية

            # =================================================================
            # الخطوة 2: محاولة معالجة الدفع إذا كان هناك payment_token
            # =================================================================
            if not payment_token_from_payload:
                logging.info(f"ℹ️ معاملة {tx_hash_hex} لا تحتوي على payment_token. انتهاء المعالجة.")
                continue

            try:
                pending_payment = await fetch_pending_payment_by_payment_token(conn, payment_token_from_payload)

                if not pending_payment:
                    logging.warning(
                        f"⚠️ لا يوجد سجل دفع مطابق لـ payment_token '{payment_token_from_payload}' من المعاملة {tx_hash_hex}.")
                    continue

                if pending_payment.get('status') != 'pending':
                    logging.info(
                        f"ℹ️ الدفعة المرتبطة بـ '{payment_token_from_payload}' تمت معالجتها بالفعل (الحالة: {pending_payment['status']}).")
                    continue

                logging.info(f"✅ تم العثور على دفعة معلقة مطابقة: ID={pending_payment['id']}. بدء التحقق من المبلغ.")

                # =================================================================
                # الخطوة 3: منطق التحقق من المبلغ وتحديث حالة الدفع
                # =================================================================
                telegram_id = int(pending_payment['telegram_id'])
                subscription_plan_id = pending_payment['subscription_plan_id']
                expected_subscription_price = await get_subscription_price(conn, subscription_plan_id)
                difference = expected_subscription_price - jetton_amount

                logging.info(
                    f"🔍 مقارنة المبالغ: المتوقع={expected_subscription_price}, المستلم={jetton_amount}, الفرق={difference}")

                acceptable_tolerance = Decimal('0.30')
                silent_tolerance = Decimal('0.15')

                status_to_set = None
                notification_details = {}

                if difference < 0:  # دفعة زائدة
                    status_to_set = "completed"
                    notification_details = {
                        "type": "payment_warning", "title": "دفعة زائدة",
                        "message": "لقد قمت بإرسال دفعة زائدة. سيتم تجديد اشتراكك. تواصل مع الدعم لاسترداد الفرق.",
                        "extra_data": {"severity": "warning", "difference": str(abs(difference))}
                    }
                elif difference > acceptable_tolerance:  # دفعة ناقصة جداً
                    status_to_set = "failed"
                    notification_details = {
                        "type": "payment_failed", "title": "فشل عملية الدفع",
                        "message": "فشل تجديد الاشتراك لأن الدفعة التي أرسلتها أقل من المبلغ المطلوب.",
                        "extra_data": {"severity": "error", "difference": str(difference)}
                    }
                elif silent_tolerance < difference <= acceptable_tolerance:  # دفعة ناقصة (مقبولة مع تحذير)
                    status_to_set = "completed"
                    notification_details = {
                        "type": "payment_warning", "title": "دفعة ناقصة ضمن الحد المسموح",
                        "message": "المبلغ المدفوع أقل من المطلوب، سنقوم بتجديد اشتراكك هذه المرة فقط.",
                        "extra_data": {"severity": "info", "difference": str(difference)}
                    }
                else:  # دفعة مناسبة
                    status_to_set = "completed"
                    notification_details = {
                        "type": "payment_success", "title": "تمت عملية الدفع بنجاح",
                        "message": "تمت عملية الدفع بنجاح.",
                        "extra_data": {"severity": "success"}
                    }

                # تحديث سجل الدفع
                updated_payment_data = await update_payment_with_txhash(
                    conn, pending_payment['payment_token'], tx_hash_hex, jetton_amount, status=status_to_set
                )

                # إرسال الإشعار
                if notification_details:
                    # دمج البيانات الأساسية مع البيانات الإضافية
                    notification_details["extra_data"].update({
                        "payment_id": tx_hash_hex, "amount": str(jetton_amount),
                        "expected_amount": str(expected_subscription_price)
                    })
                    await create_notification(
                        connection=conn, notification_type=notification_details["type"],
                        title=notification_details["title"], message=notification_details["message"],
                        extra_data=notification_details["extra_data"], is_public=False, telegram_ids=[telegram_id]
                    )

                # استدعاء API التجديد إذا نجح الدفع
                if updated_payment_data and updated_payment_data.get('status') == 'completed':
                    logging.info(f"✅ تم تحديث سجل الدفع إلى 'مكتمل'. استدعاء API التجديد...")
                    async with aiohttp.ClientSession() as session:
                        await call_subscription_api(session, telegram_id, subscription_plan_id, pending_payment)

            except Exception as e:
                logging.error(f"❌ خطأ فادح أثناء معالجة الدفع لـ token '{payment_token_from_payload}': {e}",
                              exc_info=True)


# --- 🟢 تعديل: الدالة الدورية المحدثة والمستقرة ---
async def periodic_check_payments():
    """
    تقوم هذه الدالة بالتحقق الدوري من المعاملات.
    تستخدم فحصًا حيًا للاتصال بدلاً من 'is_ready'.
    """
    logging.info("🕰️ Payment confirmation background task starting.")
    await asyncio.sleep(15)  # انتظر قليلاً بعد بدء التشغيل

    while True:
        try:
            provider = current_app.lite_balancer
            if not provider:
                logging.error("❌ LiteBalancer has not been initialized. Skipping this cycle.")
                await asyncio.sleep(60)
                continue

            # --- الفحص الجديد للجاهزية ---
            try:
                # سنحاول تنفيذ أمر أساسي. إذا نجح، فالاتصال جاهز.
                await provider.get_masterchain_info()
                logging.info("✅ LiteBalancer connection is active. Proceeding to check transactions.")
            except Exception as readiness_error:
                # إذا فشل، نسجل الخطأ وننتظر قبل المحاولة مرة أخرى.
                logging.error(f"❌ LiteBalancer readiness check failed: {readiness_error}. Skipping this cycle.")
                await asyncio.sleep(60)  # انتظر فترة أطول إذا كان الاتصال غير جاهز
                continue
            # --- نهاية الفحص الجديد ---

            # إذا نجح الفحص، نستمر في تحليل المعاملات
            await parse_transactions(provider)

        except Exception as e:
            # هذا يمسك بالأخطاء في منطق parse_transactions أو أي خطأ غير متوقع آخر
            logging.error(f"❌ Unhandled exception in the periodic check loop: {str(e)}", exc_info=True)

        logging.info("✅ Payment check cycle finished. Waiting for 20 seconds...")
        await asyncio.sleep(20)


@payment_confirmation_bp.before_app_serving
async def startup():
    """
    Starts the background task for payment checks.
    """
    logging.info("🚦 Starting the background task for payment checks...")
    if not hasattr(current_app, 'payment_check_task') or current_app.payment_check_task.done():
        current_app.payment_check_task = asyncio.create_task(periodic_check_payments())
        logging.info("✅ Payment check task has been scheduled.")


async def handle_failed_transaction(tx_hash: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await process_transaction(tx_hash)
            break
        except Exception as e:
            logging.warning(f"⚠️ محاولة {attempt + 1} فشلت: {str(e)}")
            await asyncio.sleep(5 * (attempt + 1))


@payment_confirmation_bp.route("/api/confirm_payment", methods=["POST"])
async def confirm_payment():
    logging.info("✅ تم استدعاء نقطة API /api/confirm_payment!")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة في /api/confirm_payment: {json.dumps(data, indent=2)}")

        webhook_secret_frontend = data.get("webhookSecret")
        if not webhook_secret_frontend or webhook_secret_frontend != os.getenv("WEBHOOK_SECRET"):
            logging.warning("❌ طلب غير مصرح به إلى /api/confirm_payment: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403

        user_wallet_address = data.get("userWalletAddress")
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        telegram_username = data.get("telegramUsername")
        full_name = data.get("fullName")

        logging.info(
            f"✅ استلام طلب تأكيد الدفع: userWalletAddress={user_wallet_address}, "
            f"planId={plan_id_str}, telegramId={telegram_id_str}, username={telegram_username}, full_name={full_name}"
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

        # إنشاء payment_token فريد (أرقام وحروف فقط)
        payment_token = str(uuid4()).replace('-', '')  # <--- التعديل الأول هنا

        amount = 0.0
        async with current_app.db_pool.acquire() as conn:
            try:
                user_op_successful = await add_user(
                    connection=conn,
                    telegram_id=telegram_id,
                    username=telegram_username,
                    full_name=full_name

                )
                if user_op_successful:
                    logging.info(f"👤 المستخدم {telegram_id} تم إضافته/تحديثه بنجاح في جدول users.")
                else:
                    # دالة add_user أعادت False, مما يعني خطأ داخلي تم تسجيله هناك
                    logging.warning(f"⚠️ فشلت عملية إضافة/تحديث المستخدم {telegram_id} (يرجى مراجعة سجلات add_user).")
            except Exception as e_user_update:
                logging.error(f"❌ خطأ حرج أثناء محاولة إضافة/تحديث المستخدم {telegram_id}: {str(e_user_update)}",
                              exc_info=True)

            try:
                query = "SELECT price FROM subscription_plans WHERE id = $1"
                record_price = await conn.fetchrow(query,
                                                   subscription_plan_id)  # تم تغيير اسم المتغير لتجنب التضارب مع record_payment
                if record_price and record_price.get("price") is not None:
                    amount = float(record_price["price"])
                    logging.info(f"✅ تم جلب السعر من جدول subscription_plans: {amount}")
                else:
                    logging.warning(
                        f"⚠️ لم يتم العثور على خطة بالمعرف {subscription_plan_id}. سيتم تعيين المبلغ إلى 0.0")
            except Exception as e:
                logging.error(f"❌ خطأ أثناء جلب السعر من قاعدة البيانات: {str(e)}")
                return jsonify({"error": "Internal server error"}), 500

            logging.info("💾 جاري تسجيل الدفعة المعلقة في قاعدة البيانات...")
            result = None
            max_attempts = 3
            attempt = 0

            while attempt < max_attempts:
                try:
                    # إذا كانت هذه ليست المحاولة الأولى، قم بإنشاء توكن جديد
                    # هذا يضمن أننا نستخدم التوكن الذي تم إنشاؤه خارج الحلقة للمحاولة الأولى
                    # وننشئ واحدًا جديدًا فقط في حالة حدوث تضارب وإعادة المحاولة
                    if attempt > 0:
                        payment_token = str(uuid4()).replace('-', '')  # <--- التعديل الثاني هنا (عند إعادة المحاولة)
                        logging.info(f"🔄 تم إنشاء payment_token جديد للمحاولة {attempt + 1}: {payment_token}")

                    # تأكد من أن record_payment معرفة ومستوردة بشكل صحيح
                    result = await record_payment(
                        conn=conn,
                        telegram_id=telegram_id,
                        user_wallet_address=user_wallet_address,
                        amount=amount,
                        subscription_plan_id=subscription_plan_id,
                        username=telegram_username,
                        full_name=full_name,
                        payment_token=payment_token
                    )
                    break  # اخرج من الحلقة إذا نجحت العملية
                except UniqueViolationError:
                    attempt += 1
                    logging.warning(f"⚠️ تكرار payment_token، إعادة المحاولة ({attempt}/{max_attempts})...")
                    if attempt >= max_attempts:  # إذا وصلنا للحد الأقصى للمحاولات
                        logging.error("❌ فشل تسجيل الدفعة بعد محاولات متعددة بسبب تضارب payment_token.")
                        return jsonify({"error": "Failed to record payment after retries"}), 500
                    # سيتم إنشاء payment_token جديد في بداية اللفة التالية إذا attempt > 0

            # هذا التحقق أصبح أقل أهمية هنا لأننا نتحقق من max_attempts داخل الحلقة
            # لكنه لا يزال جيدًا كإجراء وقائي إضافي
            if result is None:
                logging.error("❌ فشل تسجيل الدفعة بعد محاولات متعددة بسبب تضارب payment_token (لم يتم الوصول للنتيجة).")
                return jsonify({"error": "Failed to record payment after retries"}), 500

        logging.info(f"✅ تم تسجيل الدفعة المعلقة بنجاح في قاعدة البيانات. payment_token={payment_token}")
        formatted_amount = f"{amount:.2f}"
        return jsonify({
            "success": True,
            "payment_token": payment_token,
            "amount": formatted_amount
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
