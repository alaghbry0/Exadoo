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
        يبدأ مهمة فحص شاملة لجميع القنوات المدارة.
        """
        audit_uuid = str(uuid.uuid4())
        self.logger.info(f"Scheduling a new channel audit with UUID: {audit_uuid}")

        # لا ننشئ سجل في messaging_batches، بل سنعتمد على جدولنا الجديد channel_audits
        # ونطلق المهمة في الخلفية مباشرة
        asyncio.create_task(self._perform_channel_audit(audit_uuid))

        return audit_uuid

    async def _perform_channel_audit(self, audit_uuid: str):
        """
        المنطق الفعلي لمهمة الفحص. تعمل هذه الدالة في الخلفية.
        """
        self.logger.info(f"[{audit_uuid}] Starting audit process...")
        try:
            async with self.db_pool.acquire() as conn:
                # 1. جلب كل القنوات التي يديرها البوت
                managed_channels = await conn.fetch(
                    "SELECT DISTINCT channel_id, channel_name FROM subscription_type_channels")
                if not managed_channels:
                    self.logger.warning(f"[{audit_uuid}] No managed channels found to audit.")
                    return

                # 2. جلب كل المستخدمين المسجلين في قاعدة بياناتنا
                all_db_users_records = await conn.fetch("SELECT telegram_id FROM users")
                all_db_user_ids = {rec['telegram_id'] for rec in all_db_users_records}

                for channel in managed_channels:
                    channel_id = channel['channel_id']
                    channel_name = channel['channel_name']
                    self.logger.info(f"[{audit_uuid}] Auditing channel: {channel_name} ({channel_id})")

                    # إنشاء سجل أولي للفحص لهذه القناة
                    await conn.execute(
                        """
                        INSERT INTO channel_audits (audit_uuid, channel_id, channel_name, status)
                        VALUES ($1, $2, $3, 'RUNNING')
                        """,
                        uuid.UUID(audit_uuid), channel_id, channel_name
                    )

                    # 3. جلب عدد الأعضاء الفعلي من تيليجرام
                    try:
                        total_members_api = await self.bot.get_chat_member_count(channel_id)
                        # --- تعديل: تحديث مبكر لعدد الأعضاء ---
                        await conn.execute(
                            "UPDATE channel_audits SET total_members_api = $1 WHERE audit_uuid = $2 AND channel_id = $3",
                            total_members_api, uuid.UUID(audit_uuid), channel_id
                        )
                    except Exception as e:
                        self.logger.error(f"[{audit_uuid}] Failed to get member count for {channel_id}: {e}")
                        # تحديث حالة الفحص للفشل والمتابعة للقناة التالية
                        await conn.execute(
                            "UPDATE channel_audits SET status = 'FAILED' WHERE audit_uuid = $1 AND channel_id = $2",
                            uuid.UUID(audit_uuid), channel_id)
                        continue

                    # 4. جلب المشتركين النشطين لهذه القناة من قاعدة البيانات
                    active_subs_records = await conn.fetch(
                        "SELECT telegram_id FROM subscriptions WHERE channel_id = $1 AND is_active = TRUE AND expiry_date > NOW()",
                        channel_id
                    )
                    active_subscriber_ids = {rec['telegram_id'] for rec in active_subs_records}
                    # --- تعديل: تحديث مبكر لعدد المشتركين النشطين ---
                    await conn.execute(
                        "UPDATE channel_audits SET active_subscribers_db = $1 WHERE audit_uuid = $2 AND channel_id = $3",
                        len(active_subscriber_ids), uuid.UUID(audit_uuid), channel_id
                    )

                    # 5. البحث عن المتسللين (Freeloaders) - هذه هي العملية الطويلة
                    inactive_users_in_channel_ids = []
                    users_to_check = all_db_user_ids - active_subscriber_ids

                    for user_id in users_to_check:
                        try:
                            member = await self.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                                 ChatMemberStatus.CREATOR]:
                                inactive_users_in_channel_ids.append(user_id)
                                self.logger.debug(
                                    f"[{audit_uuid}] Found inactive user {user_id} in channel {channel_id}")
                            await asyncio.sleep(0.1)
                        except TelegramBadRequest as e:
                            if "user not found" in str(e).lower() or "participant_id_invalid" in str(e).lower():
                                pass
                            else:
                                self.logger.warning(
                                    f"[{audit_uuid}] Telegram API bad request for user {user_id} in {channel_id}: {e}")
                        except Exception as e:
                            self.logger.error(
                                f"[{audit_uuid}] Unexpected error checking member {user_id} in {channel_id}: {e}")

                    # 6. حساب الإحصائيات النهائية
                    # لاحظ أننا لم نعد بحاجة لجلب total_members_api أو active_count مرة أخرى هنا
                    active_count = len(active_subscriber_ids)
                    inactive_found_count = len(inactive_users_in_channel_ids)
                    # total_members_api تم جلبها مسبقاً
                    unidentified_count = total_members_api - active_count - inactive_found_count

                    # 7. تحديث سجل الفحص بالنتائج النهائية (بدون تكرار البيانات المحدثة مسبقًا)
                    # --- تعديل: التحديث النهائي للبيانات المتبقية فقط ---
                    await conn.execute(
                        """
                        UPDATE channel_audits
                        SET status = 'COMPLETED',
                            inactive_in_channel_db = $1,
                            unidentified_members = $2,
                            users_to_remove = $3,
                            completed_at = NOW()
                        WHERE audit_uuid = $4 AND channel_id = $5
                        """,
                        inactive_found_count,
                        max(0, unidentified_count),  # تجنب الأرقام السالبة
                        json.dumps({"ids": inactive_users_in_channel_ids}),
                        uuid.UUID(audit_uuid),
                        channel_id
                    )
                    self.logger.info(f"[{audit_uuid}] Finished auditing channel: {channel_name}. Results saved.")

        except Exception as e:
            self.logger.error(f"[{audit_uuid}] A critical error occurred during the audit process: {e}", exc_info=True)
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE channel_audits SET status = 'FAILED' WHERE audit_uuid = $1 AND status = 'RUNNING'",
                    uuid.UUID(audit_uuid))

    async def start_channel_cleanup_batch(self, audit_uuid: str, channel_id: int) -> str:
        """
        يبدأ مهمة خلفية لإزالة المستخدمين الذين تم رصدهم في فحص معين.
        """
        self.logger.info(f"Starting cleanup batch for audit {audit_uuid}, channel {channel_id}")
        async with self.db_pool.acquire() as conn:
            audit_record = await conn.fetchrow(
                "SELECT users_to_remove FROM channel_audits WHERE audit_uuid = $1 AND channel_id = $2 AND status = 'COMPLETED'",
                uuid.UUID(audit_uuid), channel_id
            )

        if not audit_record or not audit_record['users_to_remove'] or not audit_record['users_to_remove'].get('ids'):
            raise ValueError("لم يتم العثور على فحص مكتمل لهذه القناة أو لا يوجد مستخدمين لإزالتهم.")

        user_ids_to_remove = audit_record['users_to_remove']['ids']

        # تحويل قائمة الـ IDs إلى قائمة قواميس كما يتوقعها معالج المهام
        target_users = [{"telegram_id": user_id} for user_id in user_ids_to_remove]

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