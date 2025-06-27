# services/background_task_service.py

import asyncio
import logging
import uuid
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ChatMemberStatus
from utils.messaging_batch import BatchStatus, BatchType, MessagingBatchResult, FailedSendDetail
from services.background_tasks.task_processor import TaskProcessor


class BackgroundTaskService:
    """
    الواجهة العامة لبدء ومراقبة المهام في الخلفية.
    هذا الكلاس مسؤول عن:
    1. تحضير البيانات للمهمة (جلب المستخدمين).
    2. إنشاء سجل المهمة في قاعدة البيانات.
    3. تفويض المعالجة الفعلية إلى TaskProcessor.
    4. توفير دوال لاسترداد حالة المهام وإعادة المحاولة.
    """

    def __init__(self, db_pool, telegram_bot):
        self.db_pool = db_pool
        self.bot = telegram_bot
        self.logger = logging.getLogger(__name__)
        self.processor = TaskProcessor(db_pool, telegram_bot)

    # =========================================================================
    # ===   1. دوال بدء المهام (Public API)
    # =========================================================================

    async def start_channel_removal_scheduling_batch(self, subscription_type_id: int,
                                                     channels_to_schedule: List[Dict]) -> str:
        """يبدأ مهمة خلفية لجدولة إزالة المستخدمين من القنوات."""
        async with self.db_pool.acquire() as conn:
            query = """
               SELECT u.telegram_id, u.full_name, u.username, s.expiry_date
               FROM subscriptions s
               JOIN users u ON s.telegram_id = u.telegram_id
               WHERE s.subscription_type_id = $1 AND s.is_active = TRUE AND s.expiry_date > NOW()
            """
            target_users_records = await conn.fetch(query, subscription_type_id)

        if not target_users_records:
            raise ValueError("لا يوجد مشتركين نشطين لجدولة إزالتهم.")

        target_users = [dict(r) for r in target_users_records]
        context_data = {"channels_to_schedule": channels_to_schedule}

        return await self._start_task(
            batch_type=BatchType.SCHEDULE_REMOVAL,
            users=target_users,
            context_data=context_data,
            subscription_type_id=subscription_type_id
        )

    async def start_invite_batch(self, subscription_type_id: int, newly_added_channels: List[Dict],
                                 subscription_type_name: str) -> str:
        """يبدأ مهمة إرسال دعوات للمشتركين."""
        async with self.db_pool.acquire() as conn:
            query = """
               SELECT u.telegram_id, u.full_name, u.username
               FROM subscriptions s
               JOIN users u ON s.telegram_id = u.telegram_id
               WHERE s.subscription_type_id = $1 AND s.is_active = TRUE AND s.expiry_date > NOW()
            """
            target_users_records = await conn.fetch(query, subscription_type_id)

        if not target_users_records:
            raise ValueError("لا يوجد مشتركين نشطين لإرسال الدعوات لهم.")

        target_users = [dict(r) for r in target_users_records]
        context_data = {
            "channels_to_invite": newly_added_channels,
            "subscription_type_name": subscription_type_name
        }

        return await self._start_task(
            batch_type=BatchType.INVITE,
            users=target_users,
            context_data=context_data,
            subscription_type_id=subscription_type_id
        )

    async def start_enhanced_broadcast_batch(self, message_text: str, target_group: str,
                                             subscription_type_id: Optional[int] = None) -> str:
        """يبدأ مهمة بث محسنة."""
        async with self.db_pool.acquire() as connection:
            target_users_records = await self._get_target_users_for_group(connection, target_group,
                                                                          subscription_type_id)

        if not target_users_records:
            raise ValueError("لم يتم العثور على مستخدمين للمجموعة المستهدفة المحددة.")

        target_users = [dict(rec) for rec in target_users_records]
        message_content = {"text": message_text}

        return await self._start_task(
            batch_type=BatchType.BROADCAST,
            users=target_users,
            message_content=message_content,
            target_group=target_group,
            subscription_type_id=subscription_type_id
        )

    async def start_channel_audit(self) -> str:
        """
        يبدأ مهمة فحص شاملة لجميع القنوات المدارة باستخدام نظام المهام.
        """
        self.logger.info("Starting a new channel audit task.")

        async with self.db_pool.acquire() as conn:
            # جلب كل القنوات التي يديرها البوت
            managed_channels_records = await conn.fetch(
                "SELECT DISTINCT channel_id, channel_name FROM subscription_type_channels"
            )

        if not managed_channels_records:
            raise ValueError("No managed channels found to audit.")

        # قائمة القنوات التي سيتم معالجتها، عنصر واحد لكل قناة
        channels_to_audit = [dict(rec) for rec in managed_channels_records]

        # إنشاء UUID فريد لهذه العملية الشاملة
        audit_uuid = str(uuid.uuid4())

        # بيانات السياق التي سيتم تمريرها إلى المعالج
        context_data = {"audit_uuid": audit_uuid}

        # هنا نستخدم دالة _start_task العامة.
        # لاحظ أننا نمرر `channels_to_audit` إلى معامل `users`،
        # لأن `_start_task` مصمم لهذا الغرض (قائمة من العناصر للمعالجة)
        batch_id = await self._start_task(
            batch_type=BatchType.CHANNEL_AUDIT,
            users=channels_to_audit,  # نمرر القنوات هنا
            context_data=context_data,
            # لا نحتاج لـ message_content أو target_group
        )

        # يمكن أن نرجع batch_id أو audit_uuid. بما أن audit_uuid يجمع كل القنوات، فهو أفضل.
        # سنحتاج إلى ربط batch_id بـ audit_uuid في مكان ما أو تعديل الواجهة.
        # الحل الأبسط: يمكن لواجهة برمجة التطبيقات البحث عن طريق audit_uuid مباشرة في جدول channel_audits.
        return audit_uuid

    # --- دالة start_channel_cleanup_batch تحتاج تعديلاً طفيفاً ---

    async def start_channel_cleanup_batch(self, audit_uuid: str, channel_id: int) -> str:
        """
        يبدأ مهمة خلفية لإزالة المستخدمين الذين تم رصدهم في فحص معين.
        """
        self.logger.info(f"Preparing cleanup batch for audit {audit_uuid}, channel {channel_id}")

        async with self.db_pool.acquire() as conn:
            # لاحظ أننا نجلب السجل بأكمله الآن، وليس فقط عمود واحد
            audit_record_row = await conn.fetchrow(
                "SELECT users_to_remove FROM channel_audits WHERE audit_uuid = $1 AND channel_id = $2 AND status = 'COMPLETED'",
                uuid.UUID(audit_uuid), channel_id
            )

        # --- ▼▼▼▼▼ بداية التعديل ▼▼▼▼▼ ---

        if not audit_record_row or not audit_record_row['users_to_remove']:
            raise ValueError("لم يتم العثور على فحص مكتمل لهذه القناة أو لا يوجد مستخدمين لإزالتهم.")

        # استخراج بيانات users_to_remove
        users_to_remove_data = audit_record_row['users_to_remove']

        # التأكد من أن البيانات هي قاموس (إذا كانت سلسلة نصية، قم بتحليلها)
        if isinstance(users_to_remove_data, str):
            try:
                users_to_remove_data = json.loads(users_to_remove_data)
            except json.JSONDecodeError:
                raise ValueError("بيانات المستخدمين للإزالة تالفة (ليست بصيغة JSON صالحة).")

        # الآن users_to_remove_data هو بالتأكيد قاموس
        # يمكننا استخدام .get() بأمان
        if not isinstance(users_to_remove_data, dict) or not users_to_remove_data.get('ids'):
            raise ValueError("قائمة معرفات المستخدمين للإزالة مفقودة أو فارغة.")

        user_ids_to_remove = users_to_remove_data['ids']

        # --- ▲▲▲▲▲ نهاية التعديل ▲▲▲▲▲ ---

        if not user_ids_to_remove:
            raise ValueError("قائمة المستخدمين للإزالة فارغة.")

        # جلب تفاصيل المستخدمين (اختياري ولكنه مفيد للتسجيل)
        async with self.db_pool.acquire() as conn:
            users_records = await conn.fetch(
                "SELECT telegram_id, full_name, username FROM users WHERE telegram_id = ANY($1::bigint[])",
                user_ids_to_remove
            )

        target_users = [dict(rec) for rec in users_records]
        # إضافة أي مستخدم لم يتم العثور عليه في جدول users (حالة نادرة ولكن ممكنة)
        found_ids = {u['telegram_id'] for u in target_users}
        for user_id in user_ids_to_remove:
            if user_id not in found_ids:
                target_users.append({"telegram_id": user_id})

        context_data = {"channel_id": channel_id, "audit_uuid": audit_uuid}

        return await self._start_task(
            batch_type=BatchType.CHANNEL_CLEANUP,
            users=target_users,
            context_data=context_data
        )
    async def retry_failed_sends_in_batch(self, original_batch_id: str) -> str:
        """إعادة محاولة الإرسال للفاشلين في مهمة."""
        self.logger.info(f"Attempting to retry failed sends for batch {original_batch_id}")
        original_batch = await self.get_batch_status(original_batch_id)

        if not original_batch:
            raise ValueError(f"Batch with ID {original_batch_id} not found.")

        failed_sends_to_retry = [
            detail for detail in original_batch.error_details
            if detail.telegram_id and detail.is_retryable
        ]

        if not failed_sends_to_retry:
            raise ValueError("No retryable failed sends found in this batch.")

        users_for_retry = [
            {'telegram_id': fs.telegram_id, 'full_name': fs.full_name, 'username': fs.username}
            for fs in failed_sends_to_retry
        ]

        return await self._start_task(
            batch_type=original_batch.batch_type,
            users=users_for_retry,
            context_data=original_batch.context_data,
            message_content=original_batch.message_content,
            subscription_type_id=original_batch.subscription_type_id,
            target_group=original_batch.target_group
        )

    # =========================================================================
    # ===   2. دوال داخلية ومساعدة
    # =========================================================================

    async def _start_task(self, batch_type: BatchType, users: List[Dict], **kwargs) -> str:
        """دالة داخلية موحدة لبدء أي مهمة."""
        batch_id = str(uuid.uuid4())

        await self._create_batch_record(
            batch_id=batch_id,
            batch_type=batch_type,
            total_users=len(users),
            **kwargs
        )

        asyncio.create_task(
            self.processor.process_batch(
                batch_id=batch_id,
                batch_type=batch_type,
                users=users,
                context_data=kwargs.get("context_data", {}),
                message_content=kwargs.get("message_content")
            )
        )

        self.logger.info(f"Scheduled batch {batch_id} ({batch_type.value}) for {len(users)} users.")
        return batch_id

    # =========================================================================
    # ===   3. دوال التفاعل مع قاعدة البيانات
    # =========================================================================

    async def _create_batch_record(self, batch_id: str, batch_type: BatchType, total_users: int, **kwargs):
        """ينشئ سجلاً جديداً للمهمة في قاعدة البيانات."""
        async with self.db_pool.acquire() as connection:
            message_content_json = json.dumps(kwargs.get("message_content")) if "message_content" in kwargs else None
            context_data_json = json.dumps(kwargs.get("context_data")) if "context_data" in kwargs else None

            await connection.execute("""
                INSERT INTO messaging_batches (
                    batch_id, batch_type, total_users, status, created_at,
                    subscription_type_id, message_content, context_data, target_group
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
                                     batch_id, batch_type.value, total_users, BatchStatus.PENDING.value,
                                     datetime.now(timezone.utc), kwargs.get("subscription_type_id"),
                                     message_content_json, context_data_json, kwargs.get("target_group")
                                     )

    async def get_batch_status(self, batch_id: str) -> Optional[MessagingBatchResult]:
        """جلب حالة مهمة معينة."""
        async with self.db_pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT * FROM messaging_batches WHERE batch_id = $1", batch_id
            )

        if not record:
            return None

        error_details_raw = record['error_details']
        parsed_error_details = []
        if error_details_raw:
            try:
                loaded_details = json.loads(error_details_raw) if isinstance(error_details_raw,
                                                                             str) else error_details_raw
                if loaded_details:
                    for detail_dict in loaded_details:
                        if isinstance(detail_dict, dict):
                            parsed_error_details.append(FailedSendDetail(**detail_dict))
            except (json.JSONDecodeError, TypeError) as e:
                self.logger.error(f"Error parsing error_details for batch {batch_id}: {e}. Raw: {error_details_raw}")

        error_summary_raw = record['error_summary']
        parsed_error_summary = {}
        if error_summary_raw:
            try:
                parsed_error_summary = json.loads(error_summary_raw) if isinstance(error_summary_raw,
                                                                                   str) else error_summary_raw
            except (json.JSONDecodeError, TypeError):
                self.logger.error(f"Error parsing error_summary for batch {batch_id}.")

        return MessagingBatchResult(
            batch_id=record['batch_id'],
            batch_type=BatchType(record['batch_type']),
            status=BatchStatus(record['status']),
            total_users=record['total_users'],
            successful_sends=record['successful_sends'],
            failed_sends=record['failed_sends'],
            created_at=record['created_at'].replace(tzinfo=timezone.utc) if record['created_at'] else None,
            started_at=record['started_at'].replace(tzinfo=timezone.utc) if record['started_at'] else None,
            completed_at=record['completed_at'].replace(tzinfo=timezone.utc) if record['completed_at'] else None,
            subscription_type_id=record['subscription_type_id'],
            message_content=json.loads(record['message_content']) if record['message_content'] else None,
            context_data=json.loads(record['context_data']) if record['context_data'] else None,
            error_details=parsed_error_details,
            error_summary=parsed_error_summary,
            target_group=record.get('target_group')
        )

    @staticmethod
    async def _get_target_users_for_group(
            connection, target_group: str, subscription_type_id: Optional[int] = None
    ) -> List[Dict]:
        """دالة لجلب المستخدمين حسب الاستهداف مع كافة البيانات اللازمة للمتغيرات."""
        params = []

        if target_group == 'all_users':
            query = """
                        SELECT u.telegram_id, u.full_name, u.username, 
                               NULL as subscription_name, NULL as expiry_date
                        FROM users u
                    """
        elif target_group == 'no_subscription':
            query = """
                        SELECT DISTINCT u.telegram_id, u.full_name, u.username,
                               NULL as subscription_name, NULL as expiry_date
                        FROM users u 
                        LEFT JOIN subscriptions s ON u.telegram_id = s.telegram_id 
                        WHERE s.id IS NULL
                    """
        elif target_group == 'active_subscribers':
            query = """
                        SELECT DISTINCT ON (u.telegram_id) u.telegram_id, u.full_name, u.username,
                               st.name as subscription_name, s.expiry_date
                        FROM users u 
                        JOIN subscriptions s ON u.telegram_id = s.telegram_id 
                        JOIN subscription_types st ON s.subscription_type_id = st.id
                        WHERE s.is_active = true AND s.expiry_date > NOW()
                        ORDER BY u.telegram_id, s.expiry_date DESC
                    """
        elif target_group == 'expired_subscribers':
            # هذا الاستعلام يحدد المستخدمين الذين انتهى آخر اشتراك لهم وليس لديهم أي اشتراك نشط حالي
            query = """
                        SELECT DISTINCT ON (u.telegram_id) u.telegram_id, u.full_name, u.username,
                               st.name as subscription_name, s.expiry_date
                        FROM users u
                        JOIN (
                            SELECT telegram_id, MAX(expiry_date) as last_expiry
                            FROM subscriptions
                            GROUP BY telegram_id
                        ) last_sub ON u.telegram_id = last_sub.telegram_id
                        JOIN subscriptions s ON u.telegram_id = s.telegram_id AND s.expiry_date = last_sub.last_expiry
                        JOIN subscription_types st ON s.subscription_type_id = st.id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM subscriptions s2
                            WHERE s2.telegram_id = u.telegram_id AND s2.is_active = true AND s2.expiry_date > NOW()
                        )
                        ORDER BY u.telegram_id, s.expiry_date DESC
                    """
        elif target_group == 'subscription_type_active' and subscription_type_id:
            query = """
                        SELECT u.telegram_id, u.full_name, u.username,
                               st.name as subscription_name, s.expiry_date
                        FROM users u 
                        JOIN subscriptions s ON u.telegram_id = s.telegram_id 
                        JOIN subscription_types st ON s.subscription_type_id = st.id
                        WHERE s.subscription_type_id = $1 
                              AND s.is_active = true AND s.expiry_date > NOW()
                    """
            params.append(subscription_type_id)
        elif target_group == 'subscription_type_expired' and subscription_type_id:
            query = """
                        SELECT u.telegram_id, u.full_name, u.username,
                               st.name as subscription_name, s.expiry_date
                        FROM users u 
                        JOIN subscriptions s ON u.telegram_id = s.telegram_id 
                        JOIN subscription_types st ON s.subscription_type_id = st.id
                        WHERE s.subscription_type_id = $1 
                              AND s.expiry_date <= NOW()
                    """
            params.append(subscription_type_id)
        else:
            raise ValueError(f"Invalid target group or missing parameters: {target_group}")

        return await connection.fetch(query, *params)