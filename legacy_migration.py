# excel_importer.py
import pandas as pd
import asyncpg
from datetime import datetime, timezone
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from config import DATABASE_URI
except ImportError:
    logging.error("config.py or DATABASE_URI not found.")
    DATABASE_URI = os.getenv("DATABASE_URI_FALLBACK")
    if not DATABASE_URI:
        logging.error("DATABASE_URI is not available. Exiting.")
        exit(1)

EXCEL_FILE_PATH = "./May 2025 Report (2).xlsx"
USERNAME_COL = 'user name'
START_DATE_COL = 'Start Date'
EXPIRY_DATE_COL = 'Expiry date'


async def import_sheet_to_legacy(conn, excel_sheet_name: str, df: pd.DataFrame, default_sub_type_id=None):
    # قاموس لتخزين أفضل سجل (أحدث expiry_date) لكل (username, subscription_type_id)
    # المفتاح: (username_processed, sub_type_id)
    # القيمة: (original_excel_sheet, username_processed, sub_type_id, start_date, expiry_date, processed_flag)
    best_records_map = {}

    skipped_due_missing_data = 0
    skipped_due_empty_username = 0
    skipped_due_date_conversion_error = 0

    logging.info(f"بدء معالجة واختيار السجلات من ورقة: {excel_sheet_name}")

    for index, row in df.iterrows():
        original_row_data_for_logging = row.to_dict()  # للاستخدام في حالة التكرار

        username_raw = row.get(USERNAME_COL)
        start_date_raw = row.get(START_DATE_COL)
        expiry_date_raw = row.get(EXPIRY_DATE_COL)

        if pd.isna(username_raw) or pd.isna(start_date_raw) or pd.isna(expiry_date_raw):
            skipped_due_missing_data += 1
            continue

        # --- تنظيف اسم المستخدم ---
        # يفضل استخدام .lower().strip() للتوحيد وتجنب المشاكل
        # username_processed = str(username_raw).replace('@', '') # خيارك الحالي
        username_processed = str(username_raw).lower().replace('@', '').strip()  # موصى به

        if not username_processed:  # لا حاجة لـ .strip() هنا إذا استخدمته أعلاه
            skipped_due_empty_username += 1
            continue

        try:
            try:
                start_date_dt = pd.to_datetime(start_date_raw, dayfirst=True)
            except (ValueError, TypeError):
                start_date_dt = pd.to_datetime(start_date_raw, dayfirst=False)
            try:
                expiry_date_dt = pd.to_datetime(expiry_date_raw, dayfirst=True)
            except (ValueError, TypeError):
                expiry_date_dt = pd.to_datetime(expiry_date_raw, dayfirst=False)

            start_date = start_date_dt.tz_localize(None).replace(
                tzinfo=timezone.utc) if start_date_dt.tzinfo is None else start_date_dt.astimezone(timezone.utc)
            expiry_date = expiry_date_dt.tz_localize(None).replace(
                tzinfo=timezone.utc) if expiry_date_dt.tzinfo is None else expiry_date_dt.astimezone(timezone.utc)

        except Exception as e:
            skipped_due_date_conversion_error += 1
            logging.debug(  # استخدم debug هنا لتجنب إغراق السجلات إذا كانت هناك أخطاء كثيرة في التواريخ
                f"خطأ في تحويل التواريخ (سيتم تخطيه) للمستخدم '{username_processed}' في الصف {index + 2} بورقة '{excel_sheet_name}': {e}. البيانات: {original_row_data_for_logging}")
            continue

        # تحديد أنواع الاشتراكات للسجل الحالي
        current_record_sub_types = []
        if default_sub_type_id:
            current_record_sub_types.append(default_sub_type_id)
        elif excel_sheet_name.lower() == 'both':
            current_record_sub_types.extend([1, 2])  # افترض أن 1 و 2 هما معرفات الأنواع

        for sub_type_id_for_record in current_record_sub_types:
            record_key = (username_processed, sub_type_id_for_record)
            current_record_data = (
            excel_sheet_name, username_processed, sub_type_id_for_record, start_date, expiry_date, False)

            if record_key not in best_records_map:
                best_records_map[record_key] = current_record_data
            else:
                # هناك سجل موجود لنفس المستخدم والنوع، قارن expiry_date
                existing_record_data = best_records_map[record_key]
                existing_expiry_date = existing_record_data[4]  # expiry_date هو العنصر الخامس

                if expiry_date > existing_expiry_date:
                    logging.info(
                        f"تكرار لـ ({username_processed}, type:{sub_type_id_for_record}) من ورقة '{excel_sheet_name}'. "
                        f"السجل الجديد (Expiry: {expiry_date.strftime('%Y-%m-%d')}) أحدث من القديم (Expiry: {existing_expiry_date.strftime('%Y-%m-%d')}). سيتم تحديثه."
                        f" بيانات الصف الحالي من Excel: {original_row_data_for_logging}"
                    )
                    best_records_map[record_key] = current_record_data
                else:
                    logging.info(
                        f"تكرار لـ ({username_processed}, type:{sub_type_id_for_record}) من ورقة '{excel_sheet_name}'. "
                        f"السجل الجديد (Expiry: {expiry_date.strftime('%Y-%m-%d')}) ليس أحدث من القديم (Expiry: {existing_expiry_date.strftime('%Y-%m-%d')}). سيتم تجاهل السجل الجديد."
                        f" بيانات الصف الحالي من Excel: {original_row_data_for_logging}"
                    )

    if skipped_due_missing_data > 0:
        logging.warning(f"ورقة '{excel_sheet_name}': تم تخطي {skipped_due_missing_data} صف/صفوف بسبب بيانات مفقودة.")
    if skipped_due_empty_username > 0:
        logging.warning(
            f"ورقة '{excel_sheet_name}': تم تخطي {skipped_due_empty_username} صف/صفوف بسبب اسم مستخدم فارغ.")
    if skipped_due_date_conversion_error > 0:
        logging.warning(
            f"ورقة '{excel_sheet_name}': تم تخطي {skipped_due_date_conversion_error} صف/صفوف بسبب خطأ في تحويل التاريخ.")

    records_to_insert_final = list(best_records_map.values())

    if records_to_insert_final:
        # القيد unique_legacy_user_sub_type يجب أن يكون على (username, subscription_type_id)
        query = """
            INSERT INTO legacy_subscriptions (original_excel_sheet, username, subscription_type_id, start_date, expiry_date, processed)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT ON CONSTRAINT unique_legacy_user_sub_type
            DO UPDATE SET
                start_date = EXCLUDED.start_date,
                expiry_date = EXCLUDED.expiry_date,
                original_excel_sheet = EXCLUDED.original_excel_sheet,
                processed = EXCLUDED.processed; 
                
        """
        # لاحظ أن ترتيب الأعمدة في VALUES يجب أن يطابق ترتيبها في records_to_insert_final
        # records_to_insert_final يحتوي على: (original_excel_sheet, username_processed, sub_type_id_for_record, start_date, expiry_date, False)
        # الاستعلام INSERT يحتاج: (original_excel_sheet, username=$2, subscription_type_id=$3, start_date=$4, expiry_date=$5, processed=$6)
        # يجب تعديل ترتيب الأعمدة في VALUES أو في بناء records_to_insert_final.
        # دعنا نعدل بناء records_to_insert_final ليناسب الاستعلام.

        # بناء السجلات للإدخال بالترتيب الصحيح لـ INSERT:
        # (original_excel_sheet, username, subscription_type_id, start_date, expiry_date, processed_flag)
        # هذا هو الترتيب الذي استخدمناه في best_records_map[record_key] = current_record_data
        # current_record_data = (excel_sheet_name, username_processed, sub_type_id_for_record, start_date, expiry_date, False)
        # الاستعلام INSERT VALUES($1, $2, $3, $4, $5, $6)
        # $1 = original_excel_sheet
        # $2 = username
        # $3 = subscription_type_id
        # $4 = start_date
        # $5 = expiry_date
        # $6 = processed
        # الترتيب في current_record_data:
        # 0: excel_sheet_name (original_excel_sheet) -> $1
        # 1: username_processed (username) -> $2
        # 2: sub_type_id_for_record (subscription_type_id) -> $3
        # 3: start_date -> $4
        # 4: expiry_date -> $5
        # 5: False (processed) -> $6
        # الترتيب يبدو صحيحًا.

        try:
            await conn.executemany(query, records_to_insert_final)
            logging.info(
                f"تم إدخال/تحديث {len(records_to_insert_final)} سجل/سجلات فريد/ة (بناءً على أحدث تاريخ انتهاء) من ورقة '{excel_sheet_name}'.")
        except Exception as e:
            logging.error(f"خطأ أثناء إدخال/تحديث دفعة من السجلات من ورقة '{excel_sheet_name}': {e}", exc_info=True)
    else:
        logging.info(f"لا توجد سجلات صالحة للإدخال من ورقة '{excel_sheet_name}' بعد معالجة التكرارات.")


async def main_importer():
    if not os.path.exists(EXCEL_FILE_PATH):
        logging.error(f"ملف الإكسل غير موجود في المسار: {EXCEL_FILE_PATH}")
        return
    conn = None
    try:
        conn = await asyncpg.connect(DATABASE_URI)
        logging.info("تم الاتصال بقاعدة البيانات بنجاح.")

        # --- مهم: امسح الجدول قبل كل تشغيل إذا كنت تريد نتائج متسقة مع هذا المنطق ---
        # لأنه إذا كان هناك بيانات قديمة في الجدول، فإن منطق ON CONFLICT سيتفاعل معها.
        # إذا كنت تريد أن يعكس الجدول فقط "أفضل" السجلات من تشغيل Excel الحالي.
        logging.info("مسح جدول legacy_subscriptions قبل الاستيراد...")
        await conn.execute(
            "TRUNCATE TABLE legacy_subscriptions RESTART IDENTITY;")  # RESTART IDENTITY لإعادة تعيين auto-incrementing ID
        logging.info("تم مسح جدول legacy_subscriptions.")
        # --------------------------------------------------------------------------

        xls = pd.ExcelFile(EXCEL_FILE_PATH)
        sheet_mappings = {"Forex": 1, "Crypto": 2, "Both": None}

        for sheet_name_in_excel, sub_type_id in sheet_mappings.items():
            if sheet_name_in_excel in xls.sheet_names:
                logging.info(f"قراءة ورقة: {sheet_name_in_excel}...")
                df = pd.read_excel(xls, sheet_name=sheet_name_in_excel)
                await import_sheet_to_legacy(conn, sheet_name_in_excel, df, default_sub_type_id=sub_type_id)
            else:
                logging.warning(f"الورقة '{sheet_name_in_excel}' غير موجودة في ملف الإكسل. سيتم تخطيها.")
        logging.info("انتهت عملية استيراد بيانات الإكسل.")
    except Exception as e:
        logging.error(f"حدث خطأ غير متوقع أثناء عملية الاستيراد: {e}", exc_info=True)
    finally:
        if conn:
            await conn.close()
            logging.info("تم إغلاق الاتصال بقاعدة البيانات.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main_importer())