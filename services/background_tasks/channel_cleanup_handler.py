from typing import Dict, Any
from services.background_tasks.base_handler import BaseTaskHandler
from utils.db_utils import remove_users_from_channel  # تأكد من أن المسار صحيح


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