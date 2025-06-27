# services/background_tasks/broadcast_handler.py

from typing import Dict, Any

from aiogram.exceptions import TelegramBadRequest
from services.background_tasks.base_handler import BaseTaskHandler
from utils.task_helpers import replace_message_variables
from utils.db_utils import send_message_to_user


class BroadcastTaskHandler(BaseTaskHandler):
    """معالج متخصص لإرسال رسائل البث."""

    async def process_item(self, user_data: Dict[str, Any], context_data: Dict[str, Any],
                           prepared_data: Dict[str, Any]) -> None:
        message_content = context_data.get("message_content", {})
        original_text = message_content.get("text")

        if not original_text:
            raise ValueError("Broadcast message text is empty.")

        telegram_id = user_data['telegram_id']
        message_to_send = replace_message_variables(original_text, user_data)

        if not message_to_send:
            raise ValueError("Message content is unexpectedly empty after variable replacement.")

        try:
            await send_message_to_user(self.bot, telegram_id, message_to_send, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e).lower():
                self.logger.warning(
                    f"HTML parse error for user {telegram_id}. Retrying with safe name. Original error: {e}")

                # محاولة الإرسال مرة أخرى مع اسم آمن
                alt_user_data = user_data.copy()
                alt_user_data['full_name'] = "عزيزي المستخدم"
                alt_user_data['username'] = None
                alt_message = replace_message_variables(original_text, alt_user_data)

                try:
                    await send_message_to_user(self.bot, telegram_id, alt_message, parse_mode="HTML")
                    self.logger.info(f"Successfully sent alternative message to {telegram_id}.")
                except Exception as e_alt:
                    self.logger.error(f"Failed to send alternative message to {telegram_id}: {e_alt}")
                    raise e  # إعادة إثارة الخطأ الأصلي ليتم تسجيله
            else:
                raise e  # إثارة الأخطاء الأخرى من نوع BadRequest