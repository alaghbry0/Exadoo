import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from aiogram import Bot, types # types لا يزال مستخدمًا لكائنات مثل User
from aiogram.enums import TransactionPartnerType
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# DATABASE_URL يجب أن يكون المصدر الرئيسي لتفاصيل الاتصال إذا كان موجودًا
DATABASE_URL = os.getenv('DATABASE_URL')

# هذه ستُستخدم فقط إذا لم يكن DATABASE_URL موجودًا
DB_USER = os.getenv('DB_USER', 'neondb_owner')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_NAME = os.getenv('DB_NAME', 'neondb')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', 5432))
DB_SSL_MODE = os.getenv('DB_SSL_MODE') # اجعله None افتراضيًا، اعتمد على DATABASE_URL

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# --- دوال قاعدة البيانات (تبقى كما هي) ---
async def manage_user(connection: asyncpg.Connection, telegram_id: int, username: Optional[str] = None,
                      full_name: Optional[str] = None) -> Optional[int]:
    try:
        existing_user = await connection.fetchrow(
            "SELECT id, username, full_name FROM users WHERE telegram_id = $1",
            telegram_id
        )
        if existing_user:
            db_user_id = existing_user['id']
            current_username = existing_user['username']
            current_full_name = existing_user['full_name']
            new_username = username if username is not None else current_username
            new_full_name = full_name if full_name is not None else current_full_name

            if (new_username != current_username and new_username is not None) or \
                    (new_full_name != current_full_name and new_full_name is not None):
                await connection.execute(
                    "UPDATE users SET username = $1, full_name = $2 WHERE telegram_id = $3",
                    new_username, new_full_name, telegram_id
                )
                logger.info(f"User {telegram_id} data updated (DB ID: {db_user_id}).")
            else:
                logger.info(f"User {telegram_id} data is up to date (DB ID: {db_user_id}).")
            return db_user_id
        else:
            user_id = await connection.fetchval(
                "INSERT INTO users (telegram_id, username, full_name) VALUES ($1, $2, $3) RETURNING id",
                telegram_id, username, full_name
            )
            logger.info(f"New user {telegram_id} added with DB ID: {user_id}")
            return user_id
    except Exception as e:
        logger.error(f"Error managing user {telegram_id}: {e}", exc_info=True)
        return None

async def record_star_payment(
        connection: asyncpg.Connection,
        user_db_id: int,
        payer_telegram_id: int,
        subscription_plan_id: int,
        telegram_charge_id: str,
        invoice_payment_token: str,
        amount_stars: int,
        payer_username: Optional[str] = None,
        payer_full_name: Optional[str] = None,
        payment_datetime_obj: Optional[datetime] = None # هذا يجب أن يكون aware (UTC)
) -> Optional[int]:
    try:
        # payment_datetime_obj هو بالفعل aware UTC من الحلقة الرئيسية
        # إذا لم يكن موجودًا، نستخدم now(timezone.utc) وهو aware UTC
        dt_to_store = payment_datetime_obj if payment_datetime_obj else datetime.now(timezone.utc)

        # بما أن أعمدة DB هي timestamp without time zone،
        # ولا يوجد default لـ processed_at،
        # والقيمة الافتراضية لـ created_at هي now() (التي قد تكون بتوقيت الخادم المحلي Naive).
        # لتجنب التعارض، يمكننا إما:
        # 1. جعل dt_to_store naive (بعد التأكد أنه يمثل UTC)
        # 2. أو الاعتماد على asyncpg للقيام بالتحويل.
        # الخطأ "can't subtract offset-naive and offset-aware datetimes" يشير إلى أن asyncpg
        # يواجه مشكلة في التحويل أو المقارنة.

        # الخيار 1: تحويله إلى naive UTC representation
        # created_at_val_naive_utc = dt_to_store.replace(tzinfo=None) # يصبح naive، لكنه لا يزال يمثل لحظة UTC
        # processed_at_val_naive_utc = created_at_val_naive_utc

        # الخيار 2: (أبسط وقد يكون كافيًا) هو التأكد من أننا نمرر دائمًا aware UTC
        # والمشكلة قد تكون في كيفية تعامل PG مع NOW() الافتراضي.
        # دعنا نحاول تمرير aware UTC ونرى. إذا استمر الخطأ، قد نحتاج لتعديل استعلام SQL
        # أو طريقة تعيين الوقت في PG.

        # الخطأ يقول "invalid input for query argument $9"
        # $9 هو created_at_val
        # $10 هو processed_at_val

        # بما أن aiogram يعطينا datetime aware (UTC)
        # دعنا نمرره كما هو ونترك asyncpg و PostgreSQL يتعاملان معه.
        # المشكلة "can't subtract offset-naive and offset-aware datetimes" تحدث داخل pgproto.pgproto.timestamp_encode
        # هذا يعني أن المشكلة في كيفية ترميز asyncpg للكائن الـ datetime.

        # تجربة: التأكد من أن الكائن الذي نمرره هو UTC aware.
        # هذا يتم بالفعل في الحلقة الرئيسية.

        # إذا كان عمود قاعدة البيانات "timestamp without time zone"
        # وأنت تمرر له كائن datetime "aware" من بايثون (مثل UTC)،
        # فإن PostgreSQL عادةً ما تقوم بتحويله إلى توقيت الجلسة الحالي (session's timezone)
        # ثم تسقط معلومات المنطقة الزمنية.
        # إذا كانت timezone الجلسة مضبوطة بشكل صحيح (مثلاً 'UTC')، فيجب أن يكون الأمر جيدًا.

        # قد يكون الحل الأبسط هو ترك PostgreSQL تتعامل مع التوقيت بالكامل باستخدام NOW()
        # إذا كان هذا مقبولاً (أي أن created_at و processed_at يتم تسجيلهما بوقت الإدراج وليس وقت معاملة تليجرام).
        # لكننا نريد استخدام وقت معاملة تليجرام.

        # فلنجرب تعديل استعلام SQL لاستخدام `AT TIME ZONE 'UTC'` إذا كنا نمرر كائن naive
        # أو نمرر كائن aware ونثق بـ asyncpg.

        # المشكلة الحالية هي أن asyncpg لا يستطيع ترميزه.
        # دعنا نرى ما إذا كان جعل الكائن naive (بعد أن كان UTC aware) يساعد asyncpg.
        # هذا يعني أننا نخبر asyncpg "هذا هو الوقت، لا تقلق بشأن منطقته الزمنية، فقط أرسله كما هو".
        # وقاعدة البيانات (timestamp without time zone) ستأخذه كما هو.

        if dt_to_store.tzinfo is not None:
             # إذا كان aware، حوله إلى naive لكن بعد التأكد أنه يمثل UTC
             # (هذا تم في الحلقة الرئيسية، لكن للتأكيد هنا)
             dt_to_store_naive_utc = dt_to_store.astimezone(timezone.utc).replace(tzinfo=None)
        else:
             # إذا كان naive بالفعل (غير متوقع من الحلقة الرئيسية)، افترض أنه UTC
             dt_to_store_naive_utc = dt_to_store


        created_at_val = dt_to_store_naive_utc
        processed_at_val = dt_to_store_naive_utc


        payment_record_id = await connection.fetchval(
            """
            INSERT INTO payments (
                user_id, telegram_id, subscription_plan_id, amount, status, currency,
                payment_token, tx_hash, username, full_name, payment_method,
                created_at, processed_at
            ) VALUES ($1, $2, $3, $4, 'completed', 'XTR', $5, $6, $7, $8, 'Telegram Stars', $9, $10)
            RETURNING id
            """,
            user_db_id, payer_telegram_id, subscription_plan_id, amount_stars,
            invoice_payment_token, telegram_charge_id, payer_username, payer_full_name,
            created_at_val, processed_at_val # الآن نمرر كائنات naive تمثل UTC
        )
        logger.info(
            f"Payment recorded: DB ID {payment_record_id}, TG Charge ID {telegram_charge_id} for user_db_id {user_db_id}")
        return payment_record_id
    except asyncpg.exceptions.UniqueViolationError:
        logger.error(f"CRITICAL: UniqueViolationError for tx_hash {telegram_charge_id} despite pre-check. This should not happen.")
        return await connection.fetchval("SELECT id FROM payments WHERE tx_hash = $1", telegram_charge_id)
    except Exception as e:
        logger.error(f"Error recording payment for tx_hash {telegram_charge_id}: {e}", exc_info=True)
        return None

async def import_past_star_transactions(bot: Bot, db_pool: asyncpg.Pool):
    logger.info("Starting import of past Telegram Star transactions...")
    offset = 0
    limit = 100 # يمكنك تقليل هذا الرقم أثناء الاختبار (مثلاً 10) لتسريع الدورات
    grand_total_fetched = 0
    newly_recorded_count = 0
    already_existed_count = 0
    skipped_irrelevant_count = 0
    total_payments_skipped_payload_data = 0
    total_processing_errors = 0 # سيشمل هذا الآن الأخطاء التي تؤدي إلى التراجع
    batch_number = 1

    while True:
        try:
            logger.info(f"--- Batch {batch_number} ---")
            logger.info(f"Fetching transactions with offset: {offset}, limit: {limit}")
            star_transactions_result = await bot.get_star_transactions(offset=offset, limit=limit)
            transactions_batch = star_transactions_result.transactions

            if not transactions_batch:
                logger.info("No more transactions to fetch.")
                break

            current_batch_fetched = len(transactions_batch)
            grand_total_fetched += current_batch_fetched
            logger.info(f"Fetched {current_batch_fetched} transactions in this batch.")

            async with db_pool.acquire() as conn: # احصل على اتصال واحد لكل دفعة
                for star_tx in transactions_batch:
                    # ابدأ معاملة قاعدة بيانات جديدة لكل معاملة نجمية فردية
                    try:
                        async with conn.transaction(): # <--- نقل المعاملة إلى هنا
                            logger.debug(f"Processing StarTransaction ID: {star_tx.id}, Amount: {star_tx.amount}, Date: {star_tx.date}")
                            logger.debug(f"  Raw Source: {star_tx.source}")
                            logger.debug(f"  Raw Receiver: {star_tx.receiver}")

                            invoice_payload_str = None
                            source_user_info = None
                            is_relevant_transaction = False

                            # ... (نفس منطق التحقق من الصلة واستخراج البيانات) ...
                            if star_tx.source and hasattr(star_tx.source, 'type') and star_tx.source.type == TransactionPartnerType.USER:
                                logger.debug(f"  TX ID {star_tx.id}: Source is USER.")
                                if hasattr(star_tx.source, 'invoice_payload') and star_tx.source.invoice_payload:
                                    logger.debug(f"  TX ID {star_tx.id}: Source has invoice_payload.")
                                    if hasattr(star_tx.source, 'user') and star_tx.source.user:
                                        logger.debug(f"  TX ID {star_tx.id}: Source has user object.")
                                        if star_tx.receiver is None or \
                                           (hasattr(star_tx.receiver, 'type') and star_tx.receiver.type == TransactionPartnerType.BOT):
                                            logger.info(f"  TX ID {star_tx.id}: Receiver is BOT or None. Transaction IS relevant.")
                                            is_relevant_transaction = True
                                            invoice_payload_str = star_tx.source.invoice_payload
                                            source_user_info = star_tx.source.user
                                        else:
                                            logger.debug(f"  TX ID {star_tx.id}: Receiver is present ({star_tx.receiver.type if hasattr(star_tx.receiver, 'type') else 'Unknown type'}) but not BOT. Skipping.")
                                            skipped_irrelevant_count += 1
                                    else:
                                        logger.warning(f"  TX ID {star_tx.id}: Source is USER type with payload, but 'user' object is missing. Skipping.")
                                        total_payments_skipped_payload_data += 1
                                else:
                                    logger.debug(f"  TX ID {star_tx.id}: Source USER does not have invoice_payload or it's empty. Skipping.")
                                    skipped_irrelevant_count += 1
                            else:
                                logger.debug(f"  TX ID {star_tx.id}: Source is not USER type or source is None. Skipping.")
                                skipped_irrelevant_count += 1

                            if not is_relevant_transaction:
                                continue # ينتقل إلى المعاملة النجمية التالية، خارج هذه المعاملة الفرعية

                            # 2. استخراج البيانات من invoice_payload
                            try:
                                payload_dict = json.loads(invoice_payload_str)
                                payer_telegram_id_from_payload = int(payload_dict.get("userId"))
                                plan_id_from_payload = int(payload_dict.get("planId")) # قد يكون 0
                                payment_token_from_payload = payload_dict.get("paymentToken")

                                if not all([payer_telegram_id_from_payload, plan_id_from_payload is not None, payment_token_from_payload]):
                                    logger.warning(f"TX ID {star_tx.id}: Missing critical data in parsed payload: {payload_dict}. Skipping.")
                                    total_payments_skipped_payload_data +=1
                                    continue # ينتقل إلى المعاملة النجمية التالية
                                if payer_telegram_id_from_payload != source_user_info.id:
                                    logger.warning(f"TX ID {star_tx.id}: Mismatch between payload userId ({payer_telegram_id_from_payload}) and source userId ({source_user_info.id}). Skipping.")
                                    total_payments_skipped_payload_data += 1
                                    continue # ينتقل إلى المعاملة النجمية التالية
                            except (json.JSONDecodeError, ValueError, TypeError) as e:
                                logger.error(f"TX ID {star_tx.id}: Error parsing invoice_payload '{invoice_payload_str}': {e}. Skipping.")
                                # لا نزيد total_processing_errors هنا لأننا سنلتقطه في الـ except الخارجي لهذه المعاملة
                                raise # أعد إثارة الخطأ ليتم التقاطه بواسطة معالج الخطأ للمعاملة الفردية

                            # 3. إدارة المستخدم
                            user_full_name = source_user_info.full_name or f"{source_user_info.first_name} {source_user_info.last_name or ''}".strip()
                            user_db_id = await manage_user(
                                conn,
                                telegram_id=source_user_info.id,
                                username=source_user_info.username,
                                full_name=user_full_name
                            )
                            if user_db_id is None:
                                # manage_user سجل الخطأ بالفعل.
                                # نحتاج إلى إيقاف هذه المعاملة الفردية والتراجع عنها.
                                logger.error(f"TX ID {star_tx.id}: Aborting item transaction due to manage_user failure for user {source_user_info.id}.")
                                raise Exception(f"manage_user failed for user {source_user_info.id}") # سيؤدي إلى التراجع

                            # 4. تسجيل الدفعة
                            payment_datetime = star_tx.date
                            if isinstance(payment_datetime, datetime) and \
                               (payment_datetime.tzinfo is None or payment_datetime.tzinfo.utcoffset(payment_datetime) is None):
                                payment_datetime = payment_datetime.replace(tzinfo=timezone.utc)

                            existing_payment_id = await conn.fetchval(
                                "SELECT id FROM payments WHERE tx_hash = $1", star_tx.id
                            )

                            if existing_payment_id:
                                logger.info(f"Payment tx_hash {star_tx.id} already exists in DB (ID: {existing_payment_id}).")
                                already_existed_count += 1
                            else:
                                logger.info(f"Payment tx_hash {star_tx.id} is new. Attempting to record.")
                                new_payment_id = await record_star_payment(
                                    conn,
                                    user_db_id=user_db_id,
                                    payer_telegram_id=source_user_info.id,
                                    subscription_plan_id=plan_id_from_payload,
                                    telegram_charge_id=star_tx.id,
                                    invoice_payment_token=payment_token_from_payload,
                                    amount_stars=abs(star_tx.amount),
                                    payer_username=source_user_info.username,
                                    payer_full_name=user_full_name,
                                    payment_datetime_obj=payment_datetime
                                )
                                if new_payment_id is not None:
                                    newly_recorded_count += 1
                                else:
                                    # record_star_payment سجل الخطأ بالفعل.
                                    logger.error(f"TX ID {star_tx.id}: Aborting item transaction due to record_star_payment failure.")
                                    raise Exception(f"record_star_payment failed for TX {star_tx.id}") # سيؤدي إلى التراجع
                            # إذا وصلنا إلى هنا، كل شيء على ما يرام لهذه المعاملة النجمية، سيتم تنفيذ commit.

                    except Exception as item_ex:
                        # أي استثناء هنا (بما في ذلك تلك التي أُثيرت يدويًا أعلاه)
                        # سيؤدي إلى التراجع عن المعاملة الفرعية `async with conn.transaction():` الحالية.
                        logger.error(f"TX ID {star_tx.id}: Failed processing item, transaction for this item rolled back. Error: {item_ex}", exc_info=False)
                        total_processing_errors += 1
                        # تستمر الحلقة للمعاملة النجمية التالية، وستبدأ معاملة قاعدة بيانات جديدة لها.

            offset += current_batch_fetched
            batch_number += 1
            if current_batch_fetched < limit:
                logger.info("Fetched all available transactions.")
                break
            await asyncio.sleep(1.5) # لا يزال جيدًا لتقليل الضغط على API

        except TelegramAPIError as e:
            logger.error(f"Telegram API Error while fetching transactions: {e}")
            if "Too Many Requests" in str(e) or (hasattr(e, 'message') and e.message.startswith("Too Many Requests")):
                retry_after = e.parameters.get('retry_after', 60) if hasattr(e, 'parameters') and e.parameters else 60
                logger.warning(f"Rate limit hit. Waiting for {retry_after} seconds before retrying...")
                await asyncio.sleep(retry_after + 2)
            else:
                logger.exception("Unhandled Telegram API Error. Stopping.")
                break
        except Exception as e:
            logger.exception(f"An unexpected error occurred in batch processing: {e}")
            break # خطأ غير متوقع في الحلقة الرئيسية، من الأفضل التوقف

    logger.info("--- Import Summary ---")
    logger.info(f"Grand total transactions fetched from Telegram: {grand_total_fetched}")
    logger.info(f"Newly recorded payments in DB: {newly_recorded_count}")
    logger.info(f"Payments already existed in DB: {already_existed_count}")
    logger.info(f"Payments skipped due to missing/invalid payload data: {total_payments_skipped_payload_data}")
    logger.info(f"Payments skipped due to being irrelevant (e.g., not to bot, no payload, wrong source): {skipped_irrelevant_count}")
    logger.info(f"Processing errors (item transaction rolled back): {total_processing_errors}") # تم تحديث التسمية
    logger.info("Past Telegram Star transactions import process finished.")


async def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Please set it in your .env file or environment.")
        return

    db_pool = None
    try:
        if DATABASE_URL:
            logger.info(f"Connecting to database using DATABASE_URL: {DATABASE_URL.split('@')[-1]}") # اطبع جزءًا من URL
            db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        else:
            logger.info(f"Connecting to database using individual parameters: Host={DB_HOST}, DB={DB_NAME}")
            ssl_param = DB_SSL_MODE if DB_SSL_MODE and DB_SSL_MODE.lower() != 'none' else None
            db_pool = await asyncpg.create_pool(
                user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
                host=DB_HOST, port=DB_PORT, ssl=ssl_param,
                min_size=1, max_size=5
            )
        logger.info("Database connection pool established successfully.")

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            await import_past_star_transactions(bot, db_pool)
        finally:
            await bot.session.close()
            logger.info("Bot session closed.")

    except asyncpg.exceptions.InvalidPasswordError:
        logger.error("DB Connection Failed: Invalid password.")
    except asyncpg.exceptions.ClientCannotConnectError as e:
        logger.error(f"DB Connection Failed: Cannot connect to server. Details: {e}")
    except ConnectionRefusedError as e:
        logger.error(f"DB Connection Failed: Connection refused. Check host/port. Details: {e}")
    except Exception as e:
        logger.exception(f"Critical error in main execution: {e}")
    finally:
        if db_pool:
            await db_pool.close()
            logger.info("Database connection pool closed.")

if __name__ == "__main__":
    asyncio.run(main())