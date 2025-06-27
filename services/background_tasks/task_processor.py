# services/background_tasks/task_processor.py
import json
import asyncio
import logging
from datetime import datetime, timezone
from collections import Counter
from dataclasses import asdict
from typing import List, Dict, Optional, Any

from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest, TelegramAPIError

from utils.messaging_batch import BatchType, BatchStatus, FailedSendDetail
from utils.task_helpers import classify_and_translate_error

# استيراد المعالجات
from services.background_tasks.base_handler import BaseTaskHandler
from services.background_tasks.broadcast_handler import BroadcastTaskHandler
from services.background_tasks.invite_handler import InviteTaskHandler
from services.background_tasks.removal_scheduler_handler import RemovalSchedulerTaskHandler
from services.background_tasks.channel_cleanup_handler import ChannelCleanupHandler
from services.background_tasks.channel_audit_handler import ChannelAuditHandler


class TaskProcessor:
    """
    المحرك العام لمعالجة المهام في الخلفية.
    يدير الدفعات، الأخطاء العامة، والتأخير، بينما يفوض منطق المهمة الفعلي للمعالجات المتخصصة.
    """

    def __init__(self, db_pool, telegram_bot):
        self.db_pool = db_pool
        self.bot = telegram_bot
        self.logger = logging.getLogger(__name__)
        self.SEND_BATCH_SIZE = 25
        self.SEND_BATCH_DELAY_SECONDS = 1

        self.handlers: Dict[BatchType, BaseTaskHandler] = {
            BatchType.BROADCAST: BroadcastTaskHandler(db_pool, telegram_bot),
            BatchType.INVITE: InviteTaskHandler(db_pool, telegram_bot),
            BatchType.SCHEDULE_REMOVAL: RemovalSchedulerTaskHandler(db_pool, telegram_bot),
            # --- الإضافات الجديدة ---
            BatchType.CHANNEL_AUDIT: ChannelAuditHandler(db_pool, telegram_bot),
            BatchType.CHANNEL_CLEANUP: ChannelCleanupHandler(db_pool, telegram_bot),
        }

    async def process_batch(
            self,
            batch_id: str,
            batch_type: BatchType,
            users: List[Dict],
            context_data: Dict,
            message_content: Optional[Dict] = None
    ):
        self.logger.info(f"Starting processing batch {batch_id} ({batch_type.value}) for {len(users)} users.")
        await self._update_batch_status(batch_id, BatchStatus.IN_PROGRESS, started_at=datetime.now(timezone.utc))

        handler = self.handlers.get(batch_type)
        if not handler:
            self.logger.error(f"No handler found for batch type: {batch_type.value}. Aborting batch {batch_id}.")
            await self._update_final_batch_status(batch_id, 0, len(users), [], {"handler_not_found": len(users)})
            return

        total_successful = 0
        total_failed = 0
        send_errors: List[FailedSendDetail] = []

        # دمج السياق للتبسيط
        full_context = context_data.copy()
        if message_content:
            full_context['message_content'] = message_content

        try:
            prepared_data = await handler.prepare_for_batch(full_context, batch_id)
        except Exception as e:
            self.logger.error(f"Batch {batch_id}: Failed during preparation step: {e}", exc_info=True)
            error_key, msg = classify_and_translate_error(e)
            await self._update_final_batch_status(batch_id, 0, len(users), [], {error_key: len(users)})
            return

        current_batch_successful = 0
        current_batch_failed = 0

        for idx, item_data in enumerate(users):  # اسم المتغير تم تغييره للوضوح

            # يمكننا الاحتفاظ بهذا للوضوح، ولكنه ليس ضرورياً إذا كان الشرط صحيحاً
            telegram_id = item_data.get('telegram_id')

            try:
                # ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
                #               التعديل هنا
                # ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
                # الأنواع التي لا تتطلب telegram_id لكل عنصر
                types_without_user_id = [
                    BatchType.SCHEDULE_REMOVAL,
                    BatchType.CHANNEL_AUDIT
                ]

                if not telegram_id and batch_type not in types_without_user_id:
                    raise ValueError("missing telegram_id for a user-based task")
                # ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

                await handler.process_item(item_data, full_context, prepared_data)

                # المهام التي لا ترسل رسائل (مثل الجدولة) لا تحتاج لعدادات الدفعات
                if batch_type in [BatchType.BROADCAST, BatchType.INVITE]:
                    current_batch_successful += 1
                total_successful += 1

            except Exception as e:
                total_failed += 1
                if batch_type in [BatchType.BROADCAST, BatchType.INVITE]:
                    current_batch_failed += 1

                error_key, translated_message = classify_and_translate_error(e)
                is_retryable = not isinstance(e, (ValueError, TelegramForbiddenError)) and "chat not found" not in str(
                    e).lower()

                send_errors.append(FailedSendDetail(
                    telegram_id=telegram_id or 0,
                    error_message=translated_message,
                    is_retryable=is_retryable,
                    full_name=item_data.get('full_name'),
                    username = item_data.get('username'),
                    error_type = type(e).__name__,
                    error_key = error_key
                ))
                self.logger.warning(f"Batch {batch_id}: Failed to process item for user {telegram_id}. Error: {e}")

                if isinstance(e, TelegramRetryAfter):
                    wait_time = e.retry_after
                    self.logger.warning(f"Batch {batch_id}: Flood wait triggered. Waiting for {wait_time}s.")
                    await asyncio.sleep(wait_time + 1)

            is_last_item = (idx + 1) == len(users)
            is_batch_boundary = (idx + 1) % self.SEND_BATCH_SIZE == 0

            if (is_batch_boundary or is_last_item) and (current_batch_successful > 0 or current_batch_failed > 0):
                await self._update_batch_progress(batch_id, current_batch_successful, current_batch_failed)
                current_batch_successful, current_batch_failed = 0, 0

            if is_batch_boundary and not is_last_item:
                self.logger.info(
                    f"Batch {batch_id}: Processed {idx + 1}/{len(users)}, pausing for {self.SEND_BATCH_DELAY_SECONDS}s.")
                await asyncio.sleep(self.SEND_BATCH_DELAY_SECONDS)

        error_summary = dict(Counter(err.error_key for err in send_errors if err.error_key))
        await self._update_final_batch_status(batch_id, total_successful, total_failed, send_errors, error_summary)

        successful_items = [item for item in users if
                            item.get('telegram_id') not in {err.telegram_id for err in send_errors}]
        failed_items = [err for err in send_errors]

        await handler.on_batch_complete(batch_id, full_context, successful_items, failed_items)

        self.logger.info(
            f"Batch {batch_id} completed. Total Success: {total_successful}, Total Failed: {total_failed}."
        )

    async def _update_batch_progress(self, batch_id: str, successful_count: int, failed_count: int):
        """تحديث دوري لعدادات التقدم في مهمة معينة."""
        try:
            async with self.db_pool.acquire() as connection:
                await connection.execute("""
                    UPDATE messaging_batches
                    SET successful_sends = successful_sends + $1,
                        failed_sends = failed_sends + $2,
                        updated_at = NOW()
                    WHERE batch_id = $3
                """, successful_count, failed_count, batch_id)
        except Exception as e:
            self.logger.error(f"Failed to update progress for batch {batch_id}: {e}", exc_info=True)

    async def _update_batch_status(self, batch_id: str, status: BatchStatus, started_at: Optional[datetime] = None):
        """تحديث حالة المهمة (مثل البدء)."""
        async with self.db_pool.acquire() as connection:
            if status == BatchStatus.IN_PROGRESS and started_at:
                await connection.execute("""
                    UPDATE messaging_batches SET status = $1, started_at = $2
                    WHERE batch_id = $3 AND status = 'pending'
                """, status.value, started_at, batch_id)

    async def _update_final_batch_status(self, batch_id: str, total_successful: int, total_failed: int,
                                         error_details: List[FailedSendDetail], error_summary: Dict[str, int]):
        """تحديث الحالة النهائية للمهمة وتفاصيل الأخطاء بعد اكتمالها."""
        # تحديد الحالة النهائية بشكل أدق
        total_items = total_successful + total_failed
        if total_successful == 0 and total_failed > 0:
            final_status = BatchStatus.FAILED
        else:
            final_status = BatchStatus.COMPLETED

        error_details_for_db = json.dumps([asdict(detail) for detail in error_details]) if error_details else None
        error_summary_json = json.dumps(error_summary) if error_summary else None

        async with self.db_pool.acquire() as connection:
            # استخدام COALESCE لتحديث العدادات بشكل آمن في حال عدم وجود تحديثات دورية
            await connection.execute("""
                   UPDATE messaging_batches
                   SET status = $1, 
                       successful_sends = $2,
                       failed_sends = $3,
                       error_details = $4, 
                       completed_at = NOW(), 
                       error_summary = $5
                   WHERE batch_id = $6
               """,
                                     final_status.value,
                                     total_successful,
                                     total_failed,
                                     error_details_for_db,
                                     error_summary_json,
                                     batch_id
                                     )