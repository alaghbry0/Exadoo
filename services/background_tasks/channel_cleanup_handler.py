from typing import Dict, Any
from services.background_tasks.base_handler import BaseTaskHandler
from utils.db_utils import remove_users_from_channel  # تأكد من أن المسار صحيح
import json
import uuid

class ChannelCleanupHandler(BaseTaskHandler):
    """
    معالج متخصص لإزالة المستخدمين من قناة معينة بناءً على نتائج الفحص.
    """

    async def process_item(self, user_data: Dict[str, Any], context_data: Dict[str, Any],
                           prepared_data: Dict[str, Any]) -> None:
        telegram_id = user_data.get('telegram_id')
        channel_id = context_data.get('channel_id')

        if not telegram_id or not channel_id:
            raise ValueError(f"Missing telegram_id or channel_id for cleanup task. User: {user_data}")

        # استدعاء الدالة التي كتبتها بالفعل!
        success = await remove_users_from_channel(
            bot=self.bot,
            telegram_id=telegram_id,
            channel_id=channel_id
        )

        if not success:
            # إذا أرجعت الدالة False، يمكننا إثارة استثناء ليتم تسجيله كفشل
            raise RuntimeError(f"Failed to remove user {telegram_id} from channel {channel_id}")

        self.logger.info(f"Successfully removed user {telegram_id} from channel {channel_id}")

    async def on_batch_complete(self, batch_id: str, context_data: Dict[str, Any], successful_items: list,
                                failed_items: list) -> None:
        """
        بعد اكتمال مهمة الإزالة، قم بتحديث سجل الفحص الأصلي.
        """
        audit_uuid_str = context_data.get("audit_uuid")
        channel_id = context_data.get("channel_id")

        if not audit_uuid_str or not channel_id:
            self.logger.warning(
                f"[{batch_id}] Missing audit_uuid or channel_id in context, cannot update audit record.")
            return

        # قائمة بمعرفات المستخدمين الذين لم يتم إزالتهم بنجاح
        remaining_user_ids = [item.telegram_id for item in failed_items]

        self.logger.info(f"[{batch_id}] Updating audit record {audit_uuid_str} for channel {channel_id}. "
                         f"{len(remaining_user_ids)} users remain.")

        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE channel_audits
                    SET 
                        users_to_remove = jsonb_build_object('ids', $1::jsonb),
                        inactive_in_channel_db = $2
                    WHERE audit_uuid = $3 AND channel_id = $4
                    """,
                    json.dumps(remaining_user_ids),  # قائمة جديدة للمستخدمين
                    len(remaining_user_ids),  # تحديث العداد
                    uuid.UUID(audit_uuid_str),
                    channel_id
                )
            self.logger.info(f"[{batch_id}] Successfully updated audit record {audit_uuid_str}.")
        except Exception as e:
            self.logger.error(f"[{batch_id}] Failed to update audit record {audit_uuid_str}: {e}", exc_info=True)