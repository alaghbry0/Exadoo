# services/background_tasks/removal_scheduler_handler.py

from typing import Dict, Any
from database.db_queries import add_scheduled_task

from services.background_tasks.base_handler import BaseTaskHandler

class RemovalSchedulerTaskHandler(BaseTaskHandler):
    """معالج متخصص لجدولة إزالة المستخدمين من القنوات."""

    async def process_item(self, user_data: Dict[str, Any], context_data: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
        telegram_id = user_data.get('telegram_id')
        expiry_date = user_data.get('expiry_date')
        channels = context_data.get("channels_to_schedule", [])

        if not telegram_id or not expiry_date or not channels:
            # يتم تسجيل الخطأ في المعالج الرئيسي
            raise ValueError(f"Missing data for scheduling removal for user: {user_data}")

        # استخدم اتصال واحد لجدولة كل القنوات لهذا المستخدم
        async with self.db_pool.acquire() as connection:
            for channel_info in channels:
                channel_id = channel_info['channel_id']
                await add_scheduled_task(
                    connection=connection,
                    task_type="remove_user",
                    telegram_id=telegram_id,
                    channel_id=channel_id,
                    execute_at=expiry_date,
                    clean_up=True
                )
        self.logger.debug(f"Successfully scheduled removal for user {telegram_id} from {len(channels)} channels.")