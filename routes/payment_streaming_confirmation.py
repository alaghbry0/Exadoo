# routes/payment_streaming_confirmation.py
import asyncio
import logging
from utils.payment_utils import OP_JETTON_TRANSFER, JETTON_DECIMALS, normalize_address, convert_amount, OP_JETTON_TRANSFER_NOTIFICATION
from quart import Blueprint, request, jsonify, current_app
import json
from decimal import Decimal
from database.db_queries import (
    record_payment, update_payment_with_txhash,
    fetch_pending_payment_by_payment_token,
    record_incoming_transaction, update_payment_status_to_manual_check
)
from routes.payment_confirmation import process_single_transaction, get_bot_wallet_address
from asyncpg.exceptions import UniqueViolationError
import os
from typing import Optional

payment_streaming_bp = Blueprint("payment_streaming", __name__)

TONAPI_KEY = os.getenv("TONAPI_KEY")


# --- ✨ [جديد] دالة لجلب تفاصيل المعاملة من TonAPI ---

async def fetch_transaction_details(tx_hash: str) -> Optional[dict[str, any]]:
    """
    Tries to get full transaction details from TonAPI using the tx_hash.
    Includes a retry mechanism.
    """
    if not TONAPI_KEY:
        logging.error("❌ [TonAPI] TONAPI_KEY is not set. Cannot fetch transaction details.")
        return None

    url = f"https://tonapi.io/v2/blockchain/transactions/{tx_hash}"
    headers = {"Authorization": f"Bearer {TONAPI_KEY}"}

    for attempt in range(3):  # إعادة المحاولة 3 مرات
        try:
            async with current_app.aiohttp_session.get(url, headers=headers) as response:
                if response.status == 200:
                    details = await response.json()
                    logging.info(f"✅ [TonAPI] Fetched details for tx_hash: {tx_hash}")
                    return details
                elif response.status == 404:
                    logging.warning(f"⚠️ [TonAPI] Transaction {tx_hash} not found (404). It might not be indexed yet.")
                    # قد نرغب في الانتظار قليلاً وإعادة المحاولة لأن الـ indexer قد يتأخر
                else:
                    logging.error(
                        f"❌ [TonAPI] Error fetching {tx_hash}. Status: {response.status}, Body: {await response.text()}")

            # انتظار قبل إعادة المحاولة
            await asyncio.sleep(2 * (attempt + 1))

        except Exception as e:
            logging.error(f"❌ [TonAPI] Exception while fetching {tx_hash}: {e}", exc_info=True)
            await asyncio.sleep(2 * (attempt + 1))

    logging.error(f"❌ [TonAPI] All retries failed for fetching details of tx_hash: {tx_hash}")
    return None


# --- 📝 [جديد] مهمة المعالجة الخلفية للـ Webhook ---

async def handle_event_for_tx(tx_hash: str):
    """
    Background task to process a transaction received from a webhook.
    """
    logging.info(f"🚀 [Webhook Task] Starting to process tx_hash: {tx_hash}")

    details = await fetch_transaction_details(tx_hash)
    if not details:
        logging.error(f"❌ [Webhook Task] Could not fetch details for {tx_hash}. Aborting.")
        return

    try:
        in_msg = details.get("in_msg")
        if not in_msg:
            logging.info(f"ℹ️ [Webhook Task] Transaction {tx_hash} has no in_msg. Skipping.")
            return

        op_code_hex = in_msg.get("op_code")
        if not op_code_hex or int(op_code_hex, 16) not in [OP_JETTON_TRANSFER, OP_JETTON_TRANSFER_NOTIFICATION]:
            logging.info(f"ℹ️ [Webhook Task] Transaction {tx_hash} is not a relevant jetton op_code. Skipping.")
            return

        decoded_op_name = in_msg.get("decoded_op_name")
        if decoded_op_name not in ["jetton_transfer", "jetton_notify"]:
            logging.info(
                f"ℹ️ [Webhook Task] Decoded op is '{decoded_op_name}', not a relevant jetton operation. Skipping.")
            return

        # ✅ تعديل: استخدام decoded_body مباشرة إذا كان موجودًا
        decoded_body = in_msg.get("decoded_body")
        if not decoded_body:
            logging.error(f"❌ [Webhook Task] No decoded_body found for jetton operation {tx_hash}.")
            return

        # ✅ تعديل: إصلاح طريقة استخلاص البيانات لتطابق JSON الفعلي

        # 1. إصلاح استخلاص عنوان المرسل
        sender_address_raw = decoded_body.get("sender")

        # 2. إصلاح استخلاص التوكن (التعليق)
        forward_payload = decoded_body.get("forward_payload", {})
        value_obj = forward_payload.get("value", {})
        nested_value = value_obj.get("value", {})
        payment_token = nested_value.get("text")  # قد يكون None إذا لم يكن هناك تعليق

        # 3. الحصول على المبلغ
        jetton_amount_raw = decoded_body.get("amount")

        # التحقق من أن جميع البيانات الأساسية موجودة
        if not all([payment_token, jetton_amount_raw, sender_address_raw]):
            logging.warning(
                f"⚠️ [Webhook Task] Incomplete jetton data in {tx_hash}. Missing one of: payment_token, amount, sender. Data: {decoded_body}")
            return

        # تحضير البيانات للمعالج المركزي
        transaction_data = {
            "tx_hash": tx_hash,
            "jetton_amount": convert_amount(int(jetton_amount_raw), JETTON_DECIMALS),
            "sender": normalize_address(sender_address_raw),
            "payment_token": payment_token.strip()
        }

        # استدعاء المعالج المركزي
        await process_single_transaction(transaction_data)

    except Exception as e:
        logging.error(f"❌ [Webhook Task] Critical error while parsing details for {tx_hash}: {e}", exc_info=True)


# --- ✨ [جديد] المستمع الرئيسي لـ Streaming API ---

async def listen_to_tonapi_stream():
    """
    يتصل بـ TonAPI Streaming API ويستمع للأحداث الواردة بشكل مستمر.
    """
    await asyncio.sleep(10)  # انتظر قليلاً بعد بدء تشغيل التطبيق
    logging.info("🚀 [Streaming] Starting TonAPI event stream listener...")

    if not TONAPI_KEY:
        logging.error("❌ [Streaming] TONAPI_KEY is not set. Cannot start listener.")
        return

    # جلب عنوان المحفظة التي نريد مراقبتها
    account_to_watch = await get_bot_wallet_address()
    if not account_to_watch:
        logging.error("❌ [Streaming] Bot wallet address not defined. Cannot start listener.")
        return

    headers = {"Authorization": f"Bearer {TONAPI_KEY}", "Accept": "text/event-stream"}
    # نحن نهتم فقط بالمعاملات الواردة
    url = f"https://tonapi.io/v2/sse/accounts/transactions?accounts={account_to_watch}"

    while True:  # حلقة لا نهائية لإعادة الاتصال عند الفشل
        try:
            logging.info(f"🔌 [Streaming] Connecting to {url}")
            async with current_app.aiohttp_session.get(url, headers=headers, timeout=None) as response:
                if response.status != 200:
                    logging.error(
                        f"❌ [Streaming] Failed to connect. Status: {response.status}. Retrying in 30 seconds...")
                    await asyncio.sleep(30)
                    continue

                logging.info("✅ [Streaming] Successfully connected to TonAPI event stream.")

                # قراءة الأحداث سطراً بسطر
                async for line in response.content:
                    line = line.decode('utf-8').strip()
                    if line.startswith("data:"):
                        try:
                            # استخلاص بيانات JSON من السطر
                            event_data_str = line[len("data:"):].strip()
                            event_data = json.loads(event_data_str)

                            tx_hash = event_data.get("tx_hash")
                            logging.info(f"📬 [Streaming] Received event for tx_hash: {tx_hash}")

                            if tx_hash:
                                # جدولة المعالجة في الخلفية
                                asyncio.create_task(handle_event_for_tx(tx_hash))

                        except json.JSONDecodeError:
                            logging.warning(f"⚠️ [Streaming] Could not decode JSON from line: {line}")
                        except Exception as e:
                            logging.error(f"❌ [Streaming] Error processing event line: {e}", exc_info=True)

        except asyncio.CancelledError:
            logging.info("🛑 [Streaming] Listener task was cancelled.")
            break
        except Exception as e:
            logging.error(f"❌ [Streaming] Connection error: {e}. Reconnecting in 30 seconds...")

        await asyncio.sleep(30)  # انتظار قبل محاولة إعادة الاتصال


# --- 🚀 تسجيل المهمة عند بدء التشغيل ---

@payment_streaming_bp.before_app_serving
async def startup_streaming_listener():
    """
    Starts the background task for listening to the TonAPI event stream.
    """
    logging.info("🚦 [Startup] Scheduling the TonAPI streaming listener...")
    # نتأكد من عدم وجود مهمة سابقة قيد التشغيل
    if not hasattr(current_app, 'tonapi_listener_task') or current_app.tonapi_listener_task.done():
        current_app.tonapi_listener_task = asyncio.create_task(listen_to_tonapi_stream())
        logging.info("✅ [Startup] TonAPI streaming listener has been scheduled successfully.")

