import logging
import asyncio
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا
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
    ✅ تنفيذ المهام المجدولة مثل إزالة المستخدمين وإرسال التذكيرات، مع ضبط التوقيت إلى UTC.
    """
    try:
        tasks = await get_pending_tasks(connection)
        logging.info(f"🔄 عدد المهام المعلقة: {len(tasks)}.")

        current_time = datetime.now(timezone.utc)  # ✅ احصل على الوقت الحالي في UTC

        for task in tasks:
            task_id = task['id']
            task_type = task['task_type']
            telegram_id = task['telegram_id']
            channel_id = task['channel_id']
            execute_at = task['execute_at']  # ✅ هذا الآن `timezone-aware` من `get_pending_tasks`

            logging.info(f"🛠️ تنفيذ المهمة {task_id}: النوع {task_type}, المستخدم {telegram_id}, القناة {channel_id}")

            if not telegram_id or not channel_id:
                logging.warning(f"⚠️ تجاهل المهمة {task_id} بسبب بيانات غير صحيحة.")
                continue

            try:
                # ✅ تحويل execute_at إلى timezone-aware UTC إذا لم يكن كذلك
                if execute_at.tzinfo is None:
                    execute_at = execute_at.replace(tzinfo=timezone.utc)

                # ✅ التأكد من أن وقت التنفيذ الفعلي لم يمر
                if execute_at > current_time:
                    logging.info(f"⏳ تأجيل تنفيذ المهمة {task_id}، لم يحن وقتها بعد.")
                    continue

                # ✅ تنفيذ المهام بناءً على نوعها
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
        deactivated = await deactivate_subscription(connection, telegram_id, channel_id)
        if not deactivated:
            logging.warning(f"⚠️ فشل تعطيل اشتراك المستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "failed")
            return

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

        # 🔹 جلب بيانات الاشتراك
        subscription = await get_subscription(connection, telegram_id, channel_id)
        if not subscription or not subscription['is_active']:
            logging.warning(f"⚠️ الاشتراك غير نشط أو غير موجود للمستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "failed")
            return

        # 🔹 التأكد من أن `expiry_date` يحتوي على `timezone`
        expiry_date = subscription['expiry_date']
        if expiry_date.tzinfo is None:
            expiry_date = expiry_date.replace(tzinfo=timezone.utc)  # ⬅️ تأكد من أن التوقيت UTC

        # 🔹 احصل على التوقيت الحالي بنفس `timezone`
        current_time = datetime.now(timezone.utc)

        # 🔹 التأكد من أن `expiry_date` بعد `current_time`
        if expiry_date <= current_time:
            logging.warning(f"⚠️ الاشتراك للمستخدم {telegram_id} انتهى. إلغاء التذكيرات المستقبلية.")
            await connection.execute("""
                UPDATE scheduled_tasks
                SET status = 'not completed'
                WHERE telegram_id = $1 AND channel_id = $2 AND status = 'pending'
            """, telegram_id, channel_id)
            return

        # 🔹 جلب إعدادات الرسائل من قاعدة البيانات
        reminder_settings = await connection.fetchrow(
            "SELECT first_reminder_message, second_reminder_message FROM reminder_settings LIMIT 1"
        )

        if not reminder_settings:
            logging.warning("⚠️ إعدادات التذكير غير موجودة. استخدام الرسائل الافتراضية.")
            first_reminder_message = "📢 تنبيه: اشتراكك سينتهي في {expiry_date} بتوقيت الرياض. يرجى التجديد."
            second_reminder_message = "⏳ تبقى {remaining_hours} ساعة على انتهاء اشتراكك. لا تنسَ التجديد!"
        else:
            first_reminder_message = reminder_settings["first_reminder_message"]
            second_reminder_message = reminder_settings["second_reminder_message"]

        # 🔹 تجهيز رسالة التذكير
        if task_type == "first_reminder":
            local_expiry = expiry_date.astimezone(pytz.timezone("Asia/Riyadh"))  # تحويل التوقيت إلى UTC+3
            formatted_date = local_expiry.strftime('%Y/%m/%d %H:%M:%S')
            message = first_reminder_message.format(expiry_date=formatted_date)
        elif task_type == "second_reminder":
            remaining_hours = int((expiry_date - current_time).total_seconds() // 3600)
            message = second_reminder_message.format(remaining_hours=remaining_hours)
        else:
            logging.warning(f"⚠️ نوع تذكير غير معروف: {task_type}.")
            return

        # 🔹 إرسال الرسالة
        success = await send_message(telegram_id, message)
        if success:
            logging.info(f"✅ تم إرسال التذكير للمستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "completed")

            # 🔹 تحديث حالة التذكير الأول إذا تم تنفيذ الثاني بنجاح
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

#دالة مساعدة لتنسيق المدة الزمنية

async def format_timedelta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if minutes > 0 and days == 0:  # لا نعرض الدقائق إذا كان هناك أيام
        parts.append(f"{minutes} دقيقة")

    if not parts:
        return "أقل من دقيقة"

    return " و".join(parts)

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
