import logging
import asyncio
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from utils.db_utils import remove_user_from_channel, send_message_to_user
from database.db_queries import (
    get_pending_tasks,
    update_task_status,
    get_subscription,
    add_scheduled_task,
    find_lapsable_user_discounts_for_type,
    deactivate_multiple_user_discounts,
    find_active_user_discounts_by_original_discount,
    get_all_channel_ids_for_type
)
import json

# إنشاء مثيل للجدولة
scheduler = AsyncIOScheduler()


# ----------------- 🔹 تنفيذ المهام المجدولة ----------------- #

async def execute_scheduled_tasks(bot: Bot, connection):
    """
    ✅ تنفيذ المهام المجدولة مثل إزالة المستخدمين وإرسال التذكيرات، مع ضبط التوقيت إلى UTC.
    """
    try:
        tasks = await get_pending_tasks(connection)
        logging.info(f"🔄 عدد المهام المعلقة: {len(tasks)}.")

        # ⭐ قائمة بالمهام التي تتطلب وجود channel_id بشكل إلزامي
        tasks_requiring_channel = ["remove_user", "first_reminder", "second_reminder"]

        current_time = datetime.now(timezone.utc)

        for task in tasks:
            task_id = task['id']
            task_type = task['task_type']
            telegram_id = task['telegram_id']
            channel_id = task['channel_id']
            execute_at = task['execute_at']

            logging.info(f"🛠️ تنفيذ المهمة {task_id}: النوع {task_type}, المستخدم {telegram_id}, القناة {channel_id}")

            # ⭐ التحقق الذكي الجديد ⭐
            # تحقق دائمًا من وجود telegram_id
            if not telegram_id:
                logging.warning(f"⚠️ تجاهل المهمة {task_id} بسبب عدم وجود معرف تيليجرام.")
                continue

            # تحقق من وجود channel_id فقط إذا كان نوع المهمة يتطلبه
            if task_type in tasks_requiring_channel and not channel_id:
                logging.warning(f"⚠️ تجاهل المهمة {task_id} من نوع '{task_type}' لأنها تتطلب معرف قناة.")
                continue

            try:
                if execute_at.tzinfo is None:
                    execute_at = execute_at.replace(tzinfo=timezone.utc)

                if execute_at > current_time:
                    logging.info(f"⏳ تأجيل تنفيذ المهمة {task_id}، لم يحن وقتها بعد.")
                    continue

                # ✅ الآن استخدام `bot` الذي تم تمريره للدالة آمن وصحيح
                if task_type == "remove_user":
                    await handle_remove_user_task(bot, connection, telegram_id, channel_id, task_id)

                elif task_type == "deactivate_discount_grace_period":
                    await handle_deactivate_discount_task(bot, connection, task)

                elif task_type in ["first_reminder", "second_reminder"]:
                    await handle_reminder_task(bot, connection, telegram_id, task_type, task_id, channel_id)
                else:
                    logging.warning(f"⚠️ Unknown task type: {task_type}. Skipping.")

            except Exception as task_error:
                logging.error(f"❌ خطأ أثناء تنفيذ المهمة {task_id}: {task_error}", exc_info=True) # أضفت exc_info=True لتفاصيل أفضل
                await update_task_status(connection, task_id, "failed")

        logging.info("✅ تم تنفيذ جميع المهام المجدولة بنجاح.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء تنفيذ المهام المجدولة: {e}", exc_info=True) # أضفت exc_info=True




# ----------------- 🔹 معالجة مهمة إزالة المستخدم ----------------- #


# --- ⭐ تعديل دالة إزالة المستخدم ---
async def handle_remove_user_task(bot: Bot, connection, telegram_id: int, channel_id: int, task_id: int):
    """
    🔹 تعالج مهمة إزالة المستخدم.
    عندما تُنفذ، ستقوم بإزالة المستخدم من القناة الرئيسية وجميع القنوات الفرعية المرتبطة بالاشتراك.
    """
    # channel_id الذي يصل هنا هو معرف القناة الرئيسية دائمًا حسب المنطق الجديد
    try:
        # الخطوة 1: التحقق من الاشتراك الرئيسي للتأكد من أنه لم يتم تجديده
        current_sub = await get_subscription(connection, telegram_id, channel_id)
        if not current_sub:
            logging.info(
                f"✅ Skipping removal task {task_id}. Main subscription record not found for user {telegram_id} in channel {channel_id}.")
            await update_task_status(connection, task_id, "completed")
            return

        if current_sub['is_active']:
            logging.info(
                f"✅ Skipping removal task {task_id} for user {telegram_id}. Main subscription is still active.")
            await update_task_status(connection, task_id, "completed")
            return

        logging.info(f"🛠️ Proceeding with removal for user {telegram_id}. Subscription is confirmed inactive.")

        subscription_type_id = current_sub.get('subscription_type_id')
        if not subscription_type_id:
            logging.error(
                f"❌ Cannot perform full removal for task {task_id}. Missing 'subscription_type_id' on subscription record.")
            await update_task_status(connection, task_id, "failed")
            return

        # الخطوة 2: جلب كل القنوات (الرئيسية والفرعية) المرتبطة بهذا الاشتراك
        all_channels_to_remove_from = await get_all_channel_ids_for_type(connection, subscription_type_id)

        if not all_channels_to_remove_from:
            logging.warning(
                f"Could not find any associated channels for sub_type_id {subscription_type_id}. Falling back to removing from main channel {channel_id} only.")
            all_channels_to_remove_from = [channel_id]  # كإجراء احتياطي

        logging.info(
            f"Unified Removal: User {telegram_id} will be removed from {len(all_channels_to_remove_from)} channel(s).")

        # الخطوة 3: المرور على كل القنوات ومحاولة إزالة المستخدم
        for ch_id in all_channels_to_remove_from:
            try:
                removal_success = await remove_user_from_channel(bot, connection, telegram_id, ch_id)
                if removal_success:
                    logging.info(f"✅ Successfully removed user {telegram_id} from channel {ch_id}.")
                # دالة remove_user_from_channel يجب أن تعالج أخطاءها بنفسها (مثلما تفعل على الأغلب)
            except Exception as e:
                # هذا الاستثناء يمسك الأخطاء غير المتوقعة فقط
                logging.error(f"❌ Unhandled error while trying to remove user {telegram_id} from channel {ch_id}: {e}",
                              exc_info=True)

        # --- بداية منطق الخصومات (لا تغيير هنا) ---
        if current_sub.get('is_main_channel_subscription'):
            user_id = await connection.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
            if subscription_type_id and user_id:
                lapsable_discount_groups = await find_lapsable_user_discounts_for_type(connection, telegram_id,
                                                                                       subscription_type_id)
                if lapsable_discount_groups:
                    logging.info(
                        f"User {telegram_id} has {len(lapsable_discount_groups)} groups of lapsable discounts. Scheduling deactivation tasks.")
                    try:
                        await send_message_to_user(bot, telegram_id,
                                                   "🔔 تنبيه هام!\n\nلقد انتهى اشتراكك. لديك خصومات خاصة مرتبطة بهذا النوع من الاشتراك. إذا لم تقم بالتجديد خلال 7 أيام، ستفقد هذه الخصومات بشكل دائم.")
                    except Exception as msg_err:
                        logging.error(f"Could not send discount warning message to {telegram_id}: {msg_err}")
                    deactivation_time = datetime.now(timezone.utc) + timedelta(hours=168)
                    for lapsable_group in lapsable_discount_groups:
                        await add_scheduled_task(connection=connection, task_type="deactivate_discount_grace_period",
                                                 telegram_id=telegram_id, execute_at=deactivation_time,
                                                 payload={'user_id': user_id,
                                                          'discount_id': lapsable_group['original_discount_id']},
                                                 clean_up=False)
        # --- نهاية منطق الخصومات ---

        # الخطوة 4: تحديث حالة المهمة الرئيسية بعد الانتهاء من كل القنوات
        await update_task_status(connection, task_id, "completed")

    except Exception as e:
        logging.error(f"❌ Critical error during unified remove user task {task_id} for user {telegram_id}: {e}",
                      exc_info=True)
        await update_task_status(connection, task_id, "failed")



# --- ⭐ تعديل كامل لدالة معالجة مهمة إلغاء الخصم ---
async def handle_deactivate_discount_task(bot: Bot, connection, task: dict):
    task_id = task['id']
    telegram_id = task['telegram_id']
    payload_str = task.get('payload')

    try:
        payload = json.loads(payload_str) if payload_str else {}
    except json.JSONDecodeError:
        logging.error(f"Task {task_id} has invalid JSON in payload: {payload_str}")
        await update_task_status(connection, task_id, "failed")
        return

    # ⭐ التحقق من الـ payload الجديد
    user_id = payload.get('user_id')
    original_discount_id = payload.get('discount_id')

    if not user_id or not original_discount_id:
        logging.error(
            f"Task {task_id} (deactivate_discount) is missing 'user_id' or 'discount_id' in payload. Marking as failed.")
        await update_task_status(connection, task_id, "failed")
        return

    try:
        # 1. جلب تفاصيل الخصم الأصلي لمعرفة نطاقه (subscription_type_id)
        original_discount = await connection.fetchrow("SELECT * FROM discounts WHERE id = $1", original_discount_id)
        if not original_discount:
            logging.error(f"Original discount {original_discount_id} not found for task {task_id}. Cannot proceed.")
            await update_task_status(connection, task_id, "failed")
            return

        # يجب أن يكون الخصم مرتبطًا بنوع اشتراك للتحقق من التجديد
        sub_type_id = original_discount.get('applicable_to_subscription_type_id')
        if not sub_type_id:
            # إذا كان الخصم على خطة فقط، نحتاج لجلب النوع من الخطة
            plan_id = original_discount.get('applicable_to_subscription_plan_id')
            if plan_id:
                sub_type_id = await connection.fetchval(
                    "SELECT subscription_type_id FROM subscription_plans WHERE id = $1", plan_id)
            if not sub_type_id:
                logging.error(
                    f"Could not determine subscription type for discount {original_discount_id} in task {task_id}.")
                await update_task_status(connection, task_id, "failed")
                return

        # 2. التحقق مما إذا كان المستخدم قد جدد اشتراكه في هذا النوع
        has_renewed = await has_active_subscription_for_type(connection, telegram_id, sub_type_id)

        if has_renewed:
            logging.info(
                f"User {telegram_id} has renewed subscription for type {sub_type_id}. Skipping deactivation of discounts from original discount {original_discount_id}.")
        else:
            # 3. إذا لم يجدد، ابحث عن كل الخصومات الممنوحة له من هذا الخصم الأصلي
            user_discount_ids_to_deactivate = await find_active_user_discounts_by_original_discount(connection, user_id,
                                                                                                    original_discount_id)

            if not user_discount_ids_to_deactivate:
                logging.info(
                    f"User {telegram_id} had no active discounts to deactivate for original discount {original_discount_id}.")
            else:
                logging.info(
                    f"User {telegram_id} did not renew. Deactivating {len(user_discount_ids_to_deactivate)} discounts from original discount {original_discount_id}.")
                # 4. قم بإلغاء تفعيلها
                deactivated_count = await deactivate_multiple_user_discounts(connection,
                                                                             user_discount_ids_to_deactivate)

                if deactivated_count > 0:
                    logging.info(f"Successfully deactivated {deactivated_count} discounts for user {telegram_id}.")
                    # يمكنك إرسال رسالة هنا إذا أردت، لكن حسب الطلب الحالي، لا نرسل
                    # await send_message_to_user(bot, telegram_id, "لقد تم إلغاء الخصم الخاص بك لعدم تجديد الاشتراك.")

        await update_task_status(connection, task_id, "completed")
    except Exception as e:
        logging.error(f"❌ Error during deactivate discount task {task_id} for user {telegram_id}: {e}", exc_info=True)
        await update_task_status(connection, task_id, "failed")


# --- ⭐ 3. تعديل: دالة التحقق من التجديد لنوع اشتراك كامل ---
async def has_active_subscription_for_type(connection, telegram_id: int, subscription_type_id: int) -> bool:
    """
    Checks if a user has ANY active subscription for a specific subscription type.
    """
    return await connection.fetchval("""
        SELECT 1 FROM subscriptions 
        WHERE telegram_id = $1 AND subscription_type_id = $2 AND is_active = true
        LIMIT 1;
    """, telegram_id, subscription_type_id) is not None


# ----------------- 🔹 معالجة مهمة التذكير ----------------- #
async def handle_reminder_task(bot: Bot, connection, telegram_id: int, task_type: str, task_id: int, channel_id: int):
    """
    إرسال إشعار بتجديد الاشتراك للمستخدم.
    تتعامل هذه النسخة مع فشل الإرسال عن طريق التقاط الاستثناءات وتحديث حالة المهمة إلى 'failed'.
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
            expiry_date = expiry_date.replace(tzinfo=timezone.utc)

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
        # ... (نفس منطق تحديد الرسالة)
        if not reminder_settings:
            first_reminder_message = "📢 تنبيه: اشتراكك سينتهي في {expiry_date} بتوقيت الرياض. يرجى التجديد."
            second_reminder_message = "⏳ تبقى {remaining_hours} ساعة على انتهاء اشتراكك. لا تنسَ التجديد!"
        else:
            first_reminder_message = reminder_settings["first_reminder_message"]
            second_reminder_message = reminder_settings["second_reminder_message"]

        # 🔹 تجهيز رسالة التذكير
        if task_type == "first_reminder":
            local_expiry = expiry_date.astimezone(pytz.timezone("Asia/Riyadh"))
            formatted_date = local_expiry.strftime('%Y/%m/%d %H:%M:%S')
            message = first_reminder_message.format(expiry_date=formatted_date)
        elif task_type == "second_reminder":
            remaining_hours = int((expiry_date - current_time).total_seconds() // 3600)
            message = second_reminder_message.format(remaining_hours=remaining_hours)
        else:
            logging.warning(f"⚠️ نوع تذكير غير معروف: {task_type}.")
            await update_task_status(connection, task_id, "failed")  # تحديث المهمة كفاشلة
            return

        # ------------------- ✨ التغيير الرئيسي هنا ✨ -------------------
        try:
            # 🔹 محاولة إرسال الرسالة
            await send_message_to_user(bot, telegram_id, message)

            # ✅ إذا وصل الكود إلى هنا، فالإرسال نجح
            logging.info(f"✅ تم إرسال التذكير بنجاح للمستخدم {telegram_id}.")
            await update_task_status(connection, task_id, "completed")

            # 🔹 تحديث حالة التذكير الأول إذا تم تنفيذ الثاني بنجاح
            if task_type == "second_reminder":
                await connection.execute("""
                    UPDATE scheduled_tasks
                    SET status = 'completed'
                    WHERE telegram_id = $1 AND channel_id = $2 AND task_type = 'first_reminder' AND status = 'pending'
                """, telegram_id, channel_id)

        except TelegramAPIError as e:
            # ❌ إذا فشل الإرسال (لأي سبب من أسباب API)، سيتم التقاط الخطأ هنا
            # دالة send_message_to_user قد سجلت الخطأ بالتفصيل بالفعل
            logging.warning(
                f"⚠️ فشل إرسال التذكير للمستخدم {telegram_id} بسبب خطأ API: {e}. سيتم تحديث حالة المهمة إلى 'failed'.")
            await update_task_status(connection, task_id, "failed")
        # ------------------- نهاية التغيير الرئيسي -------------------

    except Exception as e:
        # هذا يلتقط الأخطاء الأخرى التي قد تحدث قبل مرحلة الإرسال (مثل خطأ في الاتصال بقاعدة البيانات)
        logging.error(f"❌ خطأ غير متوقع أثناء معالجة مهمة التذكير {task_id} للمستخدم {telegram_id}: {e}", exc_info=True)
        await update_task_status(connection, task_id, "failed")

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


async def start_scheduler(bot: Bot, db_pool):
    """
    إعداد وتشغيل الجدولة، مع تمرير التبعيات اللازمة (bot, db_pool).
    """
    logging.info("⏳ بدء تشغيل الجدولة.")

    try:
        # الدالة الداخلية التي ستُنفذ كل دقيقة
        async def scheduled_task_executor():
            if not db_pool:
                logging.warning("⚠️ لم يتم توفير db_pool. لن يتم تنفيذ المهام.")
                return

            async with db_pool.acquire() as connection:
                # ✅ تعديل: تمرير `bot` إلى دالة التنفيذ
                await execute_scheduled_tasks(bot, connection)

        # تشغيل الوظيفة المجدولة كل دقيقة
        scheduler.add_job(scheduled_task_executor, 'interval', minutes=1, id="main_task_executor")
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
