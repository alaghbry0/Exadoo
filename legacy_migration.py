import os
from dotenv import load_dotenv # تأكد من تثبيت python-dotenv: pip install python-dotenv
load_dotenv() # تحميل المتغيرات من .env أولاً

import asyncio
import asyncpg
import aiohttp
import json
from decimal import Decimal
import logging

# --- إعدادات التسجيل ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات البرنامج ---
try:
    # إذا كنت تفضل DATABASE_URI من config.py ولديك هذا الملف
    from config import DATABASE_URI
    logging.info("Loaded DATABASE_URI from config.py")
except ImportError:
    logging.info("config.py not found or DATABASE_URI not in config.py. Trying DATABASE_URI_FALLBACK from environment.")
    DATABASE_URI = os.getenv("DATABASE_URI_FALLBACK")

# الآن قم بتحميل المتغيرات الأخرى من البيئة (التي قد تكون تم تعيينها عبر .env)
SUBSCRIBE_API_URL = os.getenv("SUBSCRIBE_API_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# طباعة للتحقق (للتصحيح)
logging.debug(f"Loaded DATABASE_URI = {DATABASE_URI}")
logging.debug(f"Loaded SUBSCRIBE_API_URL = {SUBSCRIBE_API_URL}")
logging.debug(f"Loaded WEBHOOK_SECRET = {WEBHOOK_SECRET}")


if not all([DATABASE_URI, SUBSCRIBE_API_URL, WEBHOOK_SECRET]):
    missing_vars = []
    if not DATABASE_URI: missing_vars.append("DATABASE_URI (from config.py or DATABASE_URI_FALLBACK env var)")
    if not SUBSCRIBE_API_URL: missing_vars.append("SUBSCRIBE_API_URL env var")
    if not WEBHOOK_SECRET: missing_vars.append("WEBHOOK_SECRET env var")
    logging.critical(f"❌ متغيرات بيئة مطلوبة غير موجودة: {', '.join(missing_vars)}. يرجى تعيينها.")
    exit(1)



async def process_missed_overpayments():
    conn = None
    processed_count = 0
    failed_count = 0
    skipped_count = 0
    # قائمة لتخزين تفاصيل النجاح والفشل
    successful_renewals = []
    failed_renewals = []

    try:
        conn = await asyncpg.connect(DATABASE_URI)
        logging.info("✅ متصل بقاعدة البيانات.")

        # --- الخطوة 1: تحديد الدفعات المتأثرة ---
        # استخدام الاستعلام الذي قدمته والذي يعمل يدويًا
        query_affected_payments = """
        SELECT
            p.id AS payment_db_id,
            p.telegram_id,
            p.username,
            p.full_name,
            p.subscription_plan_id,
            sp.name AS plan_name,
            sp.price AS plan_price,
            p.amount_received,
            p.amount AS original_expected_amount,
            (p.amount_received - sp.price) AS overpayment_amount,
            p.status,
            p.tx_hash,
            p.payment_token,
            p.created_at,
            p.processed_at,
            p.error_message
        FROM
            payments p
        JOIN
            subscription_plans sp ON p.subscription_plan_id = sp.id
        WHERE
            p.status = 'completed'
            AND p.tx_hash IS NOT NULL
            AND p.amount_received IS NOT NULL
            AND p.amount_received > sp.price
        ORDER BY
            p.created_at ASC; -- معالجة الأقدم أولاً لتكون أكثر عدلاً إذا كان هناك حدود
        """
        # ملاحظة: إذا كان الاستعلام اليدوي الذي يعمل لك يتضمن ORDER BY p.created_at DESC
        # فقم بتغييره هنا أيضًا ليتطابق. ASC يعني الأقدم أولاً.

        logging.info("🚀 تنفيذ استعلام جلب الدفعات المتأثرة...")
        affected_payments = await conn.fetch(query_affected_payments)
        logging.info(f"🔍 تم العثور على {len(affected_payments)} دفعة زائدة محتملة لمعالجتها بناءً على الاستعلام.")

        if not affected_payments:
            logging.info("🏁 لا توجد دفعات لمعالجتها بناءً على المعايير الحالية.")
            return

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {WEBHOOK_SECRET}",
                "Content-Type": "application/json"
            }

            for payment in affected_payments:
                logging.info(
                    f"\n🔄 بدء معالجة الدفعة: DB_ID={payment['payment_db_id']}, Username='{payment['username']}', tx_hash={payment['tx_hash']}")

                # التحقق من البيانات الأساسية قبل المتابعة
                required_fields = ['tx_hash', 'telegram_id', 'subscription_plan_id', 'payment_token']
                missing_fields = [field for field in required_fields if not payment[field]]

                if missing_fields:
                    logging.warning(
                        f"⚠️ تخطي الدفعة (DB ID: {payment['payment_db_id']}) بسبب نقص البيانات: {', '.join(missing_fields)}.")
                    skipped_count += 1
                    failed_renewals.append({
                        "payment_db_id": payment['payment_db_id'],
                        "username": payment['username'],
                        "tx_hash": payment['tx_hash'],
                        "reason": f"Missing data: {', '.join(missing_fields)}"
                    })
                    continue

                payload = {
                    "telegram_id": int(payment['telegram_id']),
                    "subscription_plan_id": int(payment['subscription_plan_id']),
                    "payment_id": str(payment['tx_hash']),
                    "payment_token": str(payment['payment_token']),
                    "username": str(payment['username']) if payment['username'] else None,
                    "full_name": str(payment['full_name']) if payment['full_name'] else None,
                }

                logging.info(f"📞 استدعاء API الاشتراك بالبيانات: {json.dumps(payload, indent=2)}")

                try:
                    async with session.post(SUBSCRIBE_API_URL, json=payload, headers=headers, timeout=30) as response:
                        response_status = response.status
                        response_text = await response.text()

                        if response_status == 200:
                            logging.info(
                                f"✅ نجح استدعاء API الاشتراك لـ tx_hash: {payment['tx_hash']}. الحالة: {response_status}. الاستجابة (أول 200 حرف): {response_text[:200]}...")
                            processed_count += 1
                            successful_renewals.append({
                                "payment_db_id": payment['payment_db_id'],
                                "username": payment['username'],
                                "tx_hash": payment['tx_hash'],
                                "api_response_status": response_status
                            })

                            # تحديث سجل الدفع في قاعدة البيانات
                            # يمكنك تعديل error_message لتكون أكثر وضوحًا
                            update_message = f"Manually processed via script on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - API OK."
                            await conn.execute(
                                "UPDATE payments SET processed_at = NOW(), error_message = $2 WHERE id = $1",
                                payment['payment_db_id'], update_message
                            )
                            logging.info(
                                f"   تم تحديث سجل الدفع (DB ID: {payment['payment_db_id']}) بالرسالة: {update_message}")

                        else:
                            logging.error(
                                f"❌ فشل استدعاء API الاشتراك لـ tx_hash: {payment['tx_hash']}. الحالة: {response_status}, التفاصيل: {response_text}")
                            failed_count += 1
                            failed_renewals.append({
                                "payment_db_id": payment['payment_db_id'],
                                "username": payment['username'],
                                "tx_hash": payment['tx_hash'],
                                "reason": f"API call failed with status {response_status}",
                                "api_response_status": response_status,
                                "api_response_text": response_text
                            })
                            await conn.execute(
                                "UPDATE payments SET error_message = $2 WHERE id = $1",
                                payment['payment_db_id'],
                                f"Manual script API call failed: {response_status} - {response_text[:500]}"
                            )
                            logging.warning(f"   تم تحديث error_message لسجل الدفع (DB ID: {payment['payment_db_id']})")

                except aiohttp.ClientError as e:
                    logging.exception(
                        f"❌ خطأ في الاتصال (ClientError) بـ API الاشتراك لـ tx_hash: {payment['tx_hash']}. الخطأ: {str(e)}")
                    failed_count += 1
                    failed_renewals.append({
                        "payment_db_id": payment['payment_db_id'],
                        "username": payment['username'],
                        "tx_hash": payment['tx_hash'],
                        "reason": f"aiohttp.ClientError: {str(e)}",
                    })
                    await conn.execute(
                        "UPDATE payments SET error_message = $2 WHERE id = $1",
                        payment['payment_db_id'],
                        f"Manual script API connection error: {str(e)[:500]}"
                    )
                except asyncio.TimeoutError:
                    logging.exception(
                        f"❌ انتهت مهلة الاتصال (TimeoutError) بـ API الاشتراك لـ tx_hash: {payment['tx_hash']}.")
                    failed_count += 1
                    failed_renewals.append({
                        "payment_db_id": payment['payment_db_id'],
                        "username": payment['username'],
                        "tx_hash": payment['tx_hash'],
                        "reason": "asyncio.TimeoutError",
                    })
                    await conn.execute(
                        "UPDATE payments SET error_message = $2 WHERE id = $1",
                        payment['payment_db_id'],
                        "Manual script API timeout error"
                    )
                except Exception as e:
                    logging.exception(
                        f"❌ خطأ عام غير متوقع أثناء معالجة tx_hash: {payment['tx_hash']}. الخطأ: {str(e)}")  # .exception يطبع تتبع الخطأ
                    failed_count += 1
                    failed_renewals.append({
                        "payment_db_id": payment['payment_db_id'],
                        "username": payment['username'],
                        "tx_hash": payment['tx_hash'],
                        "reason": f"Unexpected error: {str(e)}",
                        "error_type": type(e).__name__
                    })
                    await conn.execute(
                        "UPDATE payments SET error_message = $2 WHERE id = $1",
                        payment['payment_db_id'],
                        f"Manual script unexpected error: {str(e)[:500]}"
                    )

                await asyncio.sleep(0.5)  # انتظار بسيط بين الطلبات لتجنب إغراق الـ API

    except asyncpg.PostgresError as e:
        logging.exception(f"❌ خطأ في قاعدة البيانات أثناء الاتصال أو الاستعلام الأولي: {e}")
    except Exception as e:
        logging.exception(f"❌ خطأ عام في السكربت قبل بدء حلقة المعالجة: {e}")
    finally:
        if conn:
            await conn.close()
            logging.info("📪 تم إغلاق الاتصال بقاعدة البيانات.")

        logging.info(f"\n--- ملخص المعالجة ---")
        logging.info(
            f"   إجمالي الدفعات التي تم جلبها: {len(affected_payments) if 'affected_payments' in locals() and affected_payments is not None else 'N/A'}")
        logging.info(f"   تمت معالجة الاشتراكات بنجاح: {processed_count}")
        logging.info(f"   فشلت معالجة الاشتراكات: {failed_count}")
        logging.info(f"   تم تخطي الاشتراكات (بيانات ناقصة): {skipped_count}")

        if successful_renewals:
            logging.info("\n--- تفاصيل الاشتراكات التي تم تجديدها بنجاح ---")
            for item in successful_renewals:
                logging.info(
                    f"  - DB ID: {item['payment_db_id']}, Username: '{item['username']}', tx_hash: {item['tx_hash']}, API Status: {item['api_response_status']}")

        if failed_renewals:
            logging.warning("\n--- تفاصيل الاشتراكات التي فشل تجديدها ---")
            for item in failed_renewals:
                logging.warning(
                    f"  - DB ID: {item['payment_db_id']}, Username: '{item['username']}', tx_hash: {item['tx_hash']}, Reason: {item['reason']}")
                if "api_response_status" in item:
                    logging.warning(
                        f"    API Status: {item['api_response_status']}, API Response: {item.get('api_response_text', '')[:300]}")


if __name__ == "__main__":
    from datetime import datetime  # لاستخدامها في رسالة التحديث

    # تأكد من أنك تقوم بتشغيل هذا في بيئة تم فيها تحميل متغيرات البيئة بشكل صحيح
    # إذا كنت تستخدم .env، قم بإلغاء تعليق الأسطر التالية:
    # from dotenv import load_dotenv
    # load_dotenv()
    asyncio.run(process_missed_overpayments())