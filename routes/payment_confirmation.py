# routes/payment_confirmation.py
from uuid import uuid4
import logging
import asyncio
from quart import Blueprint, request, jsonify, current_app
import json
import os
from decimal import Decimal, ROUND_DOWN, getcontext
import aiohttp
from utils.payment_utils import OP_JETTON_TRANSFER, JETTON_DECIMALS, normalize_address, convert_amount, OP_JETTON_TRANSFER_NOTIFICATION
from database.db_queries import record_payment, update_payment_with_txhash, fetch_pending_payment_by_payment_token, \
    record_incoming_transaction,  update_payment_status_to_manual_check
from pytoniq import LiteBalancer, begin_cell, Address
from pytoniq.liteclient.client import LiteServerError
from typing import Optional  # لإضافة تلميحات النوع
from routes.subscriptions import process_subscription_renewal
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
TONAPI_KEY = os.getenv("TONAPI_KEY")
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


# ==============================================================================
# 🌟 الدالة الرئيسية الجديدة لمعالجة المدفوعات 🌟
# ==============================================================================

async def process_single_transaction(transaction_data: dict[str, any]):
    """
    تعالج معاملة واحدة، تتحقق من صحة الدفعة، ثم تسلمها لنظام تجديد الاشتراك.
    """
    tx_hash = transaction_data.get("tx_hash")
    jetton_amount = transaction_data.get("jetton_amount", Decimal('0'))
    normalized_sender = transaction_data.get("sender")
    payment_token = transaction_data.get("payment_token")

    logging.info(f"--- 🔄 [Core Processor] Starting to process transaction: {tx_hash} ---")

    if not all([tx_hash, jetton_amount > 0, normalized_sender, payment_token]):
        logging.info(f"ℹ️ [Core Processor] Transaction {tx_hash} is missing required data. Skipping.")
        return

    async with current_app.db_pool.acquire() as conn:
        try:
            # الخطوة 1: تسجيل المعاملة الواردة لمنع المعالجة المزدوجة
            await record_incoming_transaction(
                conn=conn, txhash=tx_hash, sender=normalized_sender,
                amount=jetton_amount, payment_token=payment_token
            )
            logging.info(f"✅ [Core Processor] Transaction {tx_hash} recorded/verified in incoming_transactions.")
        except UniqueViolationError:
            logging.info(
                f"ℹ️ [Core Processor] Transaction {tx_hash} already recorded. Checking if it needs payment processing.")
        except Exception as e:
            logging.error(f"❌ [Core Processor] Failed to record transaction {tx_hash}: {e}", exc_info=True)
            return

        try:
            # الخطوة 2: البحث عن طلب دفع معلق يطابق الـ payment_token
            pending_payment = await fetch_pending_payment_by_payment_token(conn, payment_token)

            if not pending_payment:
                logging.warning(
                    f"⚠️ [Core Processor] No matching payment record found for payment_token '{payment_token}'.")
                return
            if pending_payment.get('status') != 'pending':
                logging.info(
                    f"ℹ️ [Core Processor] Payment for '{payment_token}' already processed (Status: {pending_payment['status']}).")
                return

            logging.info(
                f"✅ [Core Processor] Found matching pending payment: ID={pending_payment['id']}. Verifying amount.")

            telegram_id = int(pending_payment['telegram_id'])
            subscription_plan_id = pending_payment['subscription_plan_id']

            # الخطوة 3: التحقق من المبلغ المدفوع
            expected_price = await get_subscription_price(conn, subscription_plan_id)
            difference = expected_price - jetton_amount

            logging.info(
                f"🔍 [Core Processor] Amount comparison: Expected={expected_price}, Received={jetton_amount}, Difference={difference}")

            # --- تعديل: منطق مبسط لتحديد صلاحية الدفعة ---
            acceptable_tolerance = Decimal('0.30')
            silent_tolerance = Decimal('0.15')
            is_payment_valid_for_renewal = False
            notification_details = {}

            if difference > acceptable_tolerance:  # دفع مبلغ ناقص جداً (فشل فوري)
                is_payment_valid_for_renewal = False
                notification_details = {"type": "payment_failed", "title": "فشل عملية الدفع",
                                        "message": "فشل تجديد الاشتراك لأن الدفعة التي أرسلتها أقل من المبلغ المطلوب.",
                                        "extra_data": {"severity": "error", "difference": str(difference)}}
                # تحديث فوري للحالة إلى failed
                await update_payment_with_txhash(conn, payment_token, tx_hash, jetton_amount, status="failed")

            else:  # الدفعة مقبولة (زائدة، ناقصة بشكل طفيف، أو صحيحة)
                is_payment_valid_for_renewal = True
                if difference < 0:  # دفع زائد
                    notification_details = {"type": "payment_warning", "title": "دفعة زائدة",
                                            "message": "لقد قمت بإرسال دفعة زائدة. سيتم تجديد اشتراكك. تواصل مع الدعم لاسترداد الفرق.",
                                            "extra_data": {"severity": "warning", "difference": str(abs(difference))}}
                elif silent_tolerance < difference <= acceptable_tolerance:  # دفع ناقص ضمن الحد المسموح
                    notification_details = {"type": "payment_warning", "title": "دفعة ناقصة ضمن الحد المسموح",
                                            "message": "المبلغ المدفوع أقل من المطلوب، سنقوم بتجديد اشتراكك هذه المرة فقط.",
                                            "extra_data": {"severity": "info", "difference": str(difference)}}
                else:  # دفع صحيح أو بفارق بسيط
                    notification_details = {"type": "payment_success", "title": "تمت عملية الدفع بنجاح",
                                            "message": "تمت عملية الدفع بنجاح.", "extra_data": {"severity": "success"}}

            # الخطوة 4: إرسال الإشعار الفوري للمستخدم
            if notification_details:
                notification_details["extra_data"].update(
                    {"payment_id": tx_hash, "amount": str(jetton_amount), "expected_amount": str(expected_price)})
                await create_notification(connection=conn, notification_type=notification_details["type"],
                                          title=notification_details["title"],
                                          message=notification_details["message"],
                                          extra_data=notification_details["extra_data"], is_public=False,
                                          telegram_ids=[telegram_id])

            # الخطوة 5: تسليم المهمة لنظام التجديد إذا كانت الدفعة صالحة
            if is_payment_valid_for_renewal:
                logging.info(
                    f"✅ [Payment Valid] Payment for {tx_hash} is valid. Handing over to the subscription renewal system.")

                bot = current_app.bot
                if not bot:
                    logging.error("❌ [Core Processor] Bot object not found. Cannot proceed with subscription renewal.")
                    # تحديث الحالة للمراجعة اليدوية لأننا لا نستطيع المتابعة
                    await update_payment_status_to_manual_check(conn, pending_payment['payment_token'],
                                                                "Bot object not found during processing")
                    return

                # نجمع كل البيانات اللازمة ونسلمها
                payment_full_data = {
                    **pending_payment,  # يمرر كل بيانات الدفعة المعلقة
                    "tx_hash": tx_hash,
                    "amount_received": jetton_amount
                }

                # استدعاء دالة التجديد التي تحتوي على منطق إعادة المحاولة وتحديث الحالة النهائية
                await process_subscription_renewal(
                    connection=conn,
                    bot=bot,
                    payment_data=payment_full_data
                )

            else:
                logging.warning(
                    f"⚠️ [Payment Invalid] Payment for {tx_hash} is invalid (insufficient amount). Status has been set to 'failed'.")

        except Exception as e:
            logging.error(
                f"❌ [Core Processor] Critical error while processing payment for token '{payment_token}': {e}",
                exc_info=True)
            # في حالة حدوث خطأ غير متوقع، من الأفضل تحديث الحالة للمراجعة اليدوية
            try:
                await update_payment_status_to_manual_check(conn, payment_token, str(e))
            except Exception as inner_e:
                logging.error(
                    f"❌ [Core Processor] Failed to even update status to manual_check for token '{payment_token}': {inner_e}")

# --- 🛡️ المسار الاحتياطي: الفحص الدوري عبر LiteBalancer ---

async def get_transactions_with_retry(provider: LiteBalancer, address: str, count: int = 15, retries: int = 3,
                                      backoff_factor: float = 2.0) -> list:
    """
    يجلب المعاملات مع محاولة إعادة الاتصال عند الفشل.
    """
    for attempt in range(retries):
        try:
            return await provider.get_transactions(address=address, count=count)
        except Exception as e:
            if attempt < retries - 1:
                sleep_time = backoff_factor ** attempt
                logging.warning(
                    f"⚠️ [Polling] Attempt {attempt + 1}/{retries} failed to get transactions. Retrying in {sleep_time:.2f}s...")
                await asyncio.sleep(sleep_time)
            else:
                logging.error(f"❌ [Polling] All {retries} attempts failed. Could not get transactions.")
                raise
    return []


async def parse_transactions_from_polling(provider: LiteBalancer):
    """
    تفحص المعاملات عبر LiteBalancer وتمررها إلى المعالج المركزي.
    """
    logging.info("🔄 [Polling] Starting backup transaction parsing cycle...")

    my_wallet_address_raw = await get_bot_wallet_address()
    if not my_wallet_address_raw:
        logging.error("❌ [Polling] Bot wallet address not defined!")
        return

    normalized_bot_address = normalize_address(my_wallet_address_raw)

    try:
        transactions = await get_transactions_with_retry(provider=provider, address=normalized_bot_address)
    except Exception as e:
        logging.error(f"❌ [Polling] خطأ فادح أثناء جلب المعاملات: {e}", exc_info=True)
        return

    if not transactions:
        logging.info("ℹ️ [Polling] No new transactions found in this cycle.")
        return

    logging.info(f"✅ [Polling] Fetched {len(transactions)} transactions to check.")

    for tx in transactions:
        try:
            # فلترة أساسية للمعاملات الواردة فقط
            if not tx.in_msg or not tx.in_msg.is_internal or not tx.in_msg.body:
                continue

            # تأكد من أن المعاملة موجهة لمحفظتنا
            dest_addr = tx.in_msg.info.dest.to_str(1, 1, 1)
            if normalize_address(dest_addr) != normalized_bot_address:
                continue

            # تحليل body المعاملة
            body_slice = tx.in_msg.body.begin_parse()
            if body_slice.remaining_bits < 32: continue
            op_code = body_slice.load_uint(32)

            # تجاهل المعاملات التي ليست تحويل Jetton
            if op_code not in (OP_JETTON_TRANSFER, OP_JETTON_TRANSFER_NOTIFICATION):
                continue

            # استخراج بيانات المعاملة
            body_slice.load_bits(64)  # query_id
            jetton_amount_raw = body_slice.load_coins()
            sender_raw = body_slice.load_address().to_str(1, 1, 1)

            # استخراج التعليق (payment_token)
            forward_payload = body_slice.load_ref().begin_parse() if body_slice.load_bit() else body_slice

            payment_token = None
            if forward_payload.remaining_bits >= 32 and forward_payload.load_uint(32) == 0:
                payment_token = forward_payload.load_snake_string().strip()

            if not payment_token:
                continue

            # تحضير البيانات للمعالج المركزي
            transaction_data = {
                "tx_hash": tx.cell.hash.hex(),
                "jetton_amount": convert_amount(jetton_amount_raw, JETTON_DECIMALS),
                "sender": normalize_address(sender_raw),
                "payment_token": payment_token
            }
            # استدعاء المعالج المركزي لمعالجة هذه المعاملة
            await process_single_transaction(transaction_data)

        except Exception as e:
            tx_hash_hex = tx.cell.hash.hex() if tx.cell else "N/A"
            logging.error(f"❌ [Polling] فشل في تحليل معاملة {tx_hash_hex}: {e}", exc_info=True)
            continue


async def periodic_backup_check():
    """
    مهمة احتياطية للتحقق الدوري لضمان عدم تفويت أي معاملة.
    تعمل كل 10 دقائق، مع فترة انتظار أقصر عند حدوث خطأ.
    """
    logging.info("🕰️ [Polling] Starting BACKUP payment confirmation task.")
    await asyncio.sleep(120)  # انتظر دقيقتين عند بدء التشغيل

    while True:
        provider = current_app.lite_balancer
        if not provider:
            logging.error("❌ [Polling] LiteBalancer not initialized. Waiting for 5 minutes.")
            await asyncio.sleep(300)
            continue
        try:
            # === بداية التغييرات ===
            logging.info("🔄 [Polling] Starting new check cycle...")
            await provider.get_masterchain_info()
            logging.info("✅ [Polling] LiteBalancer connection is active.")
            await parse_transactions_from_polling(provider)

            logging.info("✅ [Polling] Backup check cycle finished successfully. Waiting for 10 minutes...")
            await asyncio.sleep(600)  # ⬅️ النوم الطويل بعد النجاح

        except Exception as e:
            logging.error(f"❌ [Polling] Unhandled exception in backup check loop: {e}", exc_info=True)
            logging.warning("[Polling] An error occurred. Pausing for 60 seconds before retrying...")
            await asyncio.sleep(60)  # ⬅️ النوم القصير بعد الفشل


# --- 🚀 تسجيل المهام عند بدء التشغيل ---

@payment_confirmation_bp.before_app_serving
async def startup_payment_tasks():
    """
    تبدأ مهمة الفحص الدوري الاحتياطي للمدفوعات.
    """
    logging.info("🚦 [Startup] Scheduling the backup payment check task...")
    # نتأكد من عدم وجود مهمة سابقة قيد التشغيل
    if not hasattr(current_app, 'payment_backup_task') or current_app.payment_backup_task.done():
        current_app.payment_backup_task = asyncio.create_task(periodic_backup_check())
        logging.info("✅ [Startup] Backup payment check task has been scheduled successfully.")


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
        payment_token = str(uuid4()).replace('-', '')

        amount = 0.0
        async with current_app.db_pool.acquire() as conn:

            # --- تم حذف كتلة كود تحديث/إضافة المستخدم من هنا ---

            try:
                query = "SELECT price FROM subscription_plans WHERE id = $1"
                record_price = await conn.fetchrow(query, subscription_plan_id)
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

            for attempt in range(max_attempts):
                try:
                    # استدعاء دالة التسجيل في كل محاولة
                    result = await record_payment(
                        conn=conn,
                        telegram_id=telegram_id,
                        subscription_plan_id=subscription_plan_id,
                        amount=Decimal(amount),
                        payment_token=payment_token,
                        username=telegram_username,
                        full_name=full_name,
                        user_wallet_address=user_wallet_address
                    )
                    break  # إذا نجح الاستدعاء دون خطأ، اخرج من الحلقة

                except UniqueViolationError:
                    logging.warning(f"⚠️ تكرار payment_token، المحاولة {attempt + 1}/{max_attempts})...")
                    if attempt + 1 >= max_attempts:
                        logging.error("❌ فشل تسجيل الدفعة بعد محاولات متعددة بسبب تضارب payment_token.")
                        return jsonify({"error": "Failed to generate a unique payment token"}), 500
                    # إنشاء توكن جديد للمحاولة التالية
                    payment_token = str(uuid4()).replace('-', '')
                    logging.info(f"🔄 تم إنشاء payment_token جديد: {payment_token}")

            if result is None:
                logging.error("❌ فشل تسجيل الدفعة المعلقة.")
                return jsonify({"error": "Failed to record pending payment after all retries."}), 500

            logging.info(f"✅ تم تسجيل الدفعة المعلقة بنجاح. payment_token={result['payment_token']}")

            formatted_amount = f"{amount:.2f}"
            return jsonify({
                "success": True,
                "payment_token": result['payment_token'],
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
