import os
import logging
import json
import io
import base64
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import qrcode
import requests
import httpx  # لإجراء الطلبات غير المتزامنة
from quart import Blueprint, request, jsonify, current_app
from asyncpg.exceptions import UniqueViolationError

# استيراد الدوال الخاصة بتوليد العناوين الفرعية من محفظة HD
from hd_wallet import get_child_wallet
# استيراد دالة إعادة المحاولة (tenacity) لجلب بيانات BscScan
from utils.retry import fetch_bscscan_data
# استيراد دالة التحقق من التأكيدات باستخدام web3.py
from services.confirmation_checker import is_transaction_confirmed

crypto_payment_bp = Blueprint("crypto_payments", __name__)

@crypto_payment_bp.route("/api/create-payment", methods=["POST"])
async def create_payment():
    logging.info("🔔 تم استدعاء نقطة API /api/create-payment")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة: {json.dumps(data, indent=2)}")

        # التحقق من مفتاح الويب هوك
        webhook_secret = data.get("webhookSecret")
        if not webhook_secret or webhook_secret != os.getenv("WEBHOOK_SECRET"):
            logging.warning("❌ طلب غير مصرح به: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403

        # استلام بيانات الخطة ومعرف المستخدم وبيانات إضافية
        plan_id_str = data.get("planId")
        telegram_id_str = data.get("telegramId")
        full_name = data.get("full_name")
        username = data.get("username")

        try:
            plan_id = int(plan_id_str)
        except (ValueError, TypeError):
            logging.error(f"❌ planId غير صالح: {plan_id_str}")
            return jsonify({"error": "Invalid planId"}), 400

        try:
            telegram_id = int(telegram_id_str)
        except (ValueError, TypeError):
            logging.error(f"❌ telegramId غير صالح: {telegram_id_str}")
            return jsonify({"error": "Invalid telegramId"}), 400

        async with current_app.db_pool.acquire() as conn:
            # استرجاع سعر الاشتراك من جدول subscription_plans
            plan = await conn.fetchrow("SELECT price FROM subscription_plans WHERE id = $1;", plan_id)
            if not plan:
                logging.error("❌ الخطة غير موجودة")
                return jsonify({"error": "Subscription plan not found"}), 404
            amount = plan["price"]

            # الحصول على القيمة التالية للمؤشر باستخدام SEQUENCE (payment_index_seq)
            row = await conn.fetchrow("SELECT nextval('payment_index_seq') as child_index;")
            child_index = row["child_index"]

            # توليد العنوان الفرعي باستخدام HD Wallet بناءً على child_index
            child_wallet = get_child_wallet(child_index)

            # إنشاء payment_token فريد
            payment_token = str(uuid4())

            # استخدام متغير بيئي لتحديد مدة صلاحية الدفع (افتراضي 30 دقيقة)
            expiry_minutes = int(os.getenv("PAYMENT_EXPIRY_MINUTES", 30))
            # تحويل الكائنات datetime إلى naive (إزالة tzinfo)
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)).replace(tzinfo=None)
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)

            # تسجيل سجل الدفع في جدول bnb_payments
            await conn.execute("""
                INSERT INTO bnb_payments 
                (plan_id, telegram_id, amount, deposit_address, child_index, status, expires_at, created_at, full_name, username, payment_token)
                VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10);
            """, plan_id, telegram_id, amount, child_wallet["address"], child_index,
               expires_at, created_at, full_name, username, payment_token)

            # تسجيل البيانات في جدول telegram_payments
            try:
                await conn.execute("""
                    INSERT INTO telegram_payments (payment_token, telegram_id, status, created_at)
                    VALUES ($1, $2, 'pending', CURRENT_TIMESTAMP);
                """, payment_token, telegram_id)
                logging.info(f"✅ تم تسجيل البيانات في جدول telegram_payments: {payment_token}")
            except UniqueViolationError as uv_err:
                logging.warning(f"⚠️ تكرار في telegram_payments لرمز الدفع {payment_token}: {uv_err}")
            except Exception as conn_err:
                logging.error(f"❌ خطأ أثناء تسجيل البيانات في telegram_payments: {conn_err}")

        # توليد رمز QR للعنوان
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(child_wallet["address"])
        qr.make(fit=True)
        img = qr.make_image(fill="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        response_data = {
            "deposit_address": child_wallet["address"],
            "network": "BEP-20",
            "amount": amount,
            "qr_code": f"data:image/png;base64,{qr_b64}",
            "payment_token": payment_token
        }
        logging.info("✅ تم إنشاء سجل الدفع بنجاح.")
        return jsonify(response_data), 200

    except Exception as exc:
        logging.error(f"❌ خطأ في /api/create-payment: {exc}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@crypto_payment_bp.route("/api/verify-payment", methods=["POST"])
async def verify_payment():
    logging.info("🔔 تم استدعاء نقطة API /api/verify-payment")
    try:
        data = await request.get_json()
        logging.info(f"📥 بيانات الطلب المستلمة: {json.dumps(data, indent=2)}")

        # التحقق من مفتاح الويب هوك
        webhook_secret = data.get("webhookSecret")
        if not webhook_secret or webhook_secret != os.getenv("WEBHOOK_SECRET"):
            logging.warning("❌ طلب غير مصرح به: مفتاح WEBHOOK_SECRET غير صالح أو مفقود")
            return jsonify({"error": "Unauthorized request"}), 403

        telegram_id_str = data.get("telegramId")
        deposit_address = data.get("deposit_address")
        try:
            telegram_id = int(telegram_id_str)
        except (ValueError, TypeError):
            logging.error(f"❌ telegramId غير صالح: {telegram_id_str}")
            return jsonify({"error": "Invalid telegramId"}), 400

        async with current_app.db_pool.acquire() as conn:
            # استرجاع سجل الدفع من جدول bnb_payments
            payment = await conn.fetchrow(
                "SELECT * FROM bnb_payments WHERE telegram_id = $1 AND deposit_address = $2;",
                telegram_id, deposit_address
            )
            if not payment:
                logging.error("❌ سجل الدفع غير موجود")
                return jsonify({"error": "Payment record not found"}), 404

            if payment["status"] == "confirmed":
                logging.info("✅ الدفع تم تأكيده مسبقاً")
                return jsonify({"success": True, "message": "Payment already confirmed"}), 200

            # التحقق من وصول الدفعة باستخدام BscScan API مع إعادة المحاولة
            bsc_api_key = os.getenv("BSCSCAN_API_KEY")
            if not bsc_api_key:
                logging.error("❌ BSCSCAN_API_KEY غير مُعد")
                return jsonify({"error": "Internal server configuration error"}), 500

            try:
                data_api = fetch_bscscan_data(deposit_address)
                logging.info(f"📄 بيانات BscScan الخام: {json.dumps(data_api, indent=2)}")
            except Exception as api_error:
                logging.error(f"❌ فشل جلب بيانات BscScan: {api_error}")
                return jsonify({"error": "Failed to fetch transaction data"}), 500

            transactions = data_api.get("result", [])

            # تحسين التحقق من نوع البيانات
            if not isinstance(transactions, list):
                actual_type = type(transactions).__name__
                logging.error(f"❌ تنسيق بيانات المعاملات غير صحيح. النوع المستلم: {actual_type}")
                return jsonify({
                    "error": "Invalid transaction data format",
                    "details": f"Expected list, got {actual_type}"
                }), 500

            # تسجيل معلومات إضافية للمساعدة في التشخيص
            logging.info(f"🔍 عدد المعاملات المستلمة: {len(transactions)}")
            if transactions:
                logging.debug(f"عينة من المعاملات: {transactions[:2]}")

            tx_found = None
            for tx_item in transactions:
                if isinstance(tx_item, dict):
                    # تحسين عملية البحث مع تسجيل تفاصيل أكثر
                    token_symbol = tx_item.get("tokenSymbol", "Unknown")
                    to_address = tx_item.get("to", "").lower()
                    value = float(tx_item.get("value", 0)) / 1e6

                    logging.debug(f"🔍 فحص معاملة: {token_symbol} -> {to_address} بقيمة {value}")

                    if (token_symbol == "USDT" and
                            to_address == deposit_address.lower() and
                            value >= payment["amount"]):
                        tx_found = tx_item
                        logging.info("✅ تم العثور على معاملة مطابقة")
                        break
                else:
                    logging.warning(f"⚠️ عنصر غير متوقع من النوع: {type(tx_item).__name__}")

            if not tx_found:
                logging.warning("❌ لم يتم العثور على معاملة مطابقة أو لم يتم تأكيد الدفعة بعد")
                return jsonify({"error": "Payment not received or not yet confirmed"}), 402

            tx_hash = tx_found.get("hash")
            if not is_transaction_confirmed(tx_hash, required_confirmations=12):
                logging.info("⏳ المعاملة موجودة لكن لم تصل لعدد التأكيدات المطلوبة بعد")
                return jsonify({"error": "Payment not confirmed yet"}), 402

            # تحديث حالة الدفع إلى confirmed
            await conn.execute("UPDATE bnb_payments SET status = 'confirmed' WHERE deposit_address = $1;", deposit_address)
            logging.info("✅ تم تأكيد الدفع.")

            # إرسال طلب تجديد الاشتراك إلى /api/subscribe
            subscribe_payload = {
                "telegram_id": telegram_id,
                "subscription_plan_id": payment["plan_id"],
                "payment_id": deposit_address,  # استخدام deposit_address كمعرف الدفع
                "payment_token": payment["payment_token"],
                "username": payment["username"],
                "full_name": payment["full_name"]
            }
            subscribe_url = os.getenv("SUBSCRIBE_URL", "http://localhost:5000")
            headers = {"Authorization": f"Bearer {os.getenv('WEBHOOK_SECRET')}"}

            async with httpx.AsyncClient() as client:
                subscribe_response = await client.post(
                    f"{subscribe_url}/api/subscribe",
                    json=subscribe_payload,
                    headers=headers
                )
            if subscribe_response.status_code != 200:
                logging.error(f"❌ فشل طلب الاشتراك: {subscribe_response.text}")
                return jsonify({"error": "Subscription renewal failed"}), 500

            subscribe_data = subscribe_response.json()
            logging.info(f"✅ تم تجديد الاشتراك بنجاح: {subscribe_data}")

            return jsonify({
                "success": True,
                "message": "Payment confirmed and subscription renewed",
                "subscription": subscribe_data
            }), 200

    except Exception as exc:
        logging.error(f"❌ خطأ في /api/verify-payment: {exc}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
