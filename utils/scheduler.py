import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from utils.db_utils import remove_user_from_channel, send_message
from config import TELEGRAM_BOT_TOKEN
from database.db_queries import (
    get_pending_tasks,
    update_task_status,
    get_subscription,
    deactivate_subscription
)

# تهيئة بوت تيليجرام
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# إنشاء مثيل للجدولة
scheduler = AsyncIOScheduler()


# ----------------- 🔹 تنفيذ المهام المجدولة ----------------- #

async def execute_scheduled_tasks(connection):
    """
    تنفيذ المهام المجدولة مثل إزالة المستخدمين وإرسال التذكيرات.
    """
    try:
        tasks = await get_pending_tasks(connection)
        logging.info(f"🔄 عدد المهام المعلقة: {len(tasks)}.")

        for task in tasks:
            task_id = task['id']
            task_type = task['task_type']
            telegram_id = task['telegram_id']
            channel_id = task['channel_id']

            logging.info(f"🛠️ تنفيذ المهمة {task_id}: النوع {task_type}, المستخدم {telegram_id}, القناة {channel_id}")

            if not telegram_id or not channel_id:
                logging.warning(f"⚠️ تجاهل المهمة {task_id} بسبب بيانات غير صحيحة.")
                continue

            try:
                if task_type == "remove_user":
                    await handle_remove_user_task(connection, telegram_id, channel_id, task_id)
                elif task_type in ["first_reminder", "second_reminder"]:
                    await handle_reminder_task(connection, telegram_id, task_type, task_id, channel_id)
                else:
                    logging.warning(f"⚠️ نوع المهمة غير معروف: {task_type}. تجاهل المهمة.")
            except Exception as task_error:
                logging.error(f"❌ خطأ أثناء تنفيذ المهمة {task_id}: {task_error}")
                await update_task_status(connection, task_id, "failed")

        logging.info("✅ تم تنفيذ جميع المهام المجدولة بنجاح.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء تنفيذ المهام المجدولة: {e}")


# ----------------- 🔹 معالجة مهمة إزالة المستخدم ----------------- #

async def handle_remove_user_task(connection, telegram_id, channel_id, task_id):
    """
    إزالة المستخدم من القناة بعد انتهاء الاشتراك.
    """
    try:
        logging.info(f"🛠️ محاولة إزالة المستخدم {telegram_id} من القناة {channel_id}.")

        # تعطيل الاشتراك في قاعدة البيانات
        await deactivate_subscription(connection, telegram_id, channel_id)

        # إزالة المستخدم من القناة
        removal_success = await remove_user_from_channel(connection, telegram_id, channel_id)
        if removal_success:
            logging.info(f"✅ تمت إزالة المستخدم {telegram_id} بنجاح.")
        else:
            logging.warning(f"⚠️ فشل إزالة المستخدم {telegram_id} من القناة {channel_id}.")

        # تحديث حالة المهمة
        await update_task_status(connection, task_id, "completed")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء إزالة المستخدم {telegram_id}: {e}")


# ----------------- 🔹 معالجة مهمة التذكير ----------------- #

async def handle_reminder_task(connection, telegram_id: int, task_type: str, task_id: int, channel_id: int):
    """
    إرسال إشعار بتجديد الاشتراك للمستخدم قبل انتهاء الاشتراك.
    """
    try:
        logging.info(f"📩 تنفيذ تذكير {task_id} للمستخدم {telegram_id}.")

        # جلب بيانات الاشتراك
        subscription = await get_subscription(connection, telegram_id, channel_id)
        if not subscription or not subscription['is_active']:
            logging.warning(f"⚠️ الاشتراك غير نشط أو غير موجود للمستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "failed")
            return

        expiry_date = subscription['expiry_date']
        current_time = datetime.now()

        if expiry_date <= current_time:
            logging.warning(f"⚠️ الاشتراك للمستخدم {telegram_id} انتهى. إلغاء التذكيرات المستقبلية.")
            await connection.execute("""
                UPDATE scheduled_tasks
                SET status = 'not completed'
                WHERE telegram_id = $1 AND channel_id = $2 AND status = 'pending'
            """, telegram_id, channel_id)
            return

        if task_type == "first_reminder":
            message = f"📢 تنبيه: اشتراكك سينتهي في {expiry_date.strftime('%Y/%m/%d %H:%M:%S')}. يرجى التجديد."
        elif task_type == "second_reminder":
            remaining_hours = int((expiry_date - current_time).total_seconds() // 3600)
            message = f"⏳ تبقى {remaining_hours} ساعة على انتهاء اشتراكك. لا تنسَ التجديد!"
        else:
            logging.warning(f"⚠️ نوع تذكير غير معروف: {task_type}.")
            return

        success = await send_message(telegram_id, message)
        if success:
            logging.info(f"✅ تم إرسال التذكير للمستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "completed")
            if task_type == "second_reminder":
                await connection.execute("""
                    UPDATE scheduled_tasks
                    SET status = 'completed'
                    WHERE telegram_id = $1 AND channel_id = $2 AND task_type = 'first_reminder' AND status = 'pending'
                """, telegram_id, channel_id)
        else:
            logging.warning(f"⚠️ فشل إرسال التذكير للمستخدم {telegram_id}.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء إرسال التذكير للمستخدم {telegram_id}: {e}")


# ----------------- 🔹 بدء الجدولة ----------------- #

async def start_scheduler(connection):

    """
    إعداد الجدولة باستخدام APScheduler وتشغيل `execute_scheduled_tasks()` كل دقيقة.
    """
    logging.info("⏳ بدء تشغيل الجدولة.")

    try:
        async def scheduled_task_executor():
            if connection:
                await execute_scheduled_tasks(connection)
            else:
                logging.warning("⚠️ لم يتم توفير اتصال بقاعدة البيانات. لن يتم تنفيذ المهام.")

        # تشغيل الوظيفة المجدولة كل دقيقة
        scheduler.add_job(scheduled_task_executor, 'interval', minutes=1)
        scheduler.start()
        logging.info("✅ تم تشغيل الجدولة بنجاح.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء بدء الجدولة: {e}")


# ----------------- 🔹 إيقاف الجدولة عند إنهاء التطبيق ----------------- #

async def shutdown_scheduler():
    """
    إيقاف الجدولة عند إيقاف التطبيق.
    """
    try:
        scheduler.shutdown()
        logging.info("🛑 تم إيقاف الجدولة بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إيقاف الجدولة: {e}")
