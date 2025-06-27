# services/background_tasks/invite_handler.py

from typing import Dict, Any, Optional, List

from services.background_tasks.base_handler import BaseTaskHandler
from utils.task_helpers import sanitize_for_html
from utils.db_utils import generate_shared_invite_link_for_channel, send_message_to_user


class InviteTaskHandler(BaseTaskHandler):
    """معالج متخصص لإرسال دعوات القنوات الجديدة."""

    async def prepare_for_batch(self, context_data: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
        """إنشاء جميع روابط الدعوة مرة واحدة لتجنب تكرار الطلبات."""
        links_map = {}
        channels_to_invite = context_data.get("channels_to_invite", [])
        subscription_name = context_data.get('subscription_type_name', 'Your Subscription')

        for ch_info in channels_to_invite:
            channel_id = ch_info.get('channel_id')
            channel_name = ch_info.get('channel_name', f"Channel {channel_id}")
            if not channel_id:
                self.logger.error(f"Missing channel_id in context_data for batch {batch_id}: {ch_info}")
                continue
            try:
                res = await generate_shared_invite_link_for_channel(
                    self.bot, int(channel_id), channel_name,
                    link_name_prefix=f"Invite for {subscription_name}"
                )
                if res and res.get("success") and res.get("invite_link"):
                    links_map[channel_id] = res["invite_link"]
                else:
                    error_msg = res.get('error', 'Unknown error generating link')
                    self.logger.error(
                        f"Failed to generate invite link for channel {channel_id} ('{channel_name}') in batch {batch_id}: {error_msg}")
            except Exception as e:
                self.logger.error(f"Exception generating invite link for channel {channel_id} in batch {batch_id}: {e}",
                                  exc_info=True)

        if not links_map and channels_to_invite:
            self.logger.warning(
                f"Batch {batch_id}: No invite links were generated, invite messages might be incomplete or fail.")

        return {"links_map": links_map}

    async def process_item(self, user_data: Dict[str, Any], context_data: Dict[str, Any],
                           prepared_data: Dict[str, Any]) -> None:
        links_map = prepared_data.get("links_map", {})
        if not context_data.get("channels_to_invite"):
            # لا يوجد شيء لإرساله
            return

        if not links_map:
            raise ValueError("Cannot send invite: No valid invite links were generated for channels.")

        message_to_send = await self._build_invite_message(user_data, context_data, links_map)

        if not message_to_send:
            raise ValueError("Message content is unexpectedly empty before sending.")

        telegram_id = user_data['telegram_id']
        await send_message_to_user(self.bot, telegram_id, message_to_send, parse_mode="HTML")

    async def _build_invite_message(self, user_data: Dict, context_data: Dict, links_map: Dict) -> Optional[str]:
        raw_full_name = user_data.get('full_name')
        raw_username = user_data.get('username')
        telegram_id_str = str(user_data.get('telegram_id', 'N/A'))

        identifier = raw_full_name or (f"@{raw_username}" if raw_username else telegram_id_str)
        user_identifier_safe = sanitize_for_html(identifier)
        subscription_name_safe = sanitize_for_html(context_data.get("subscription_type_name", "your subscription"))

        channel_links_html: List[str] = []
        channels_to_invite = context_data.get("channels_to_invite", [])

        for ch_info in channels_to_invite:
            ch_id = ch_info.get("channel_id")
            link = links_map.get(ch_id)
            if link:
                ch_name_safe = sanitize_for_html(ch_info.get("channel_name", f"Channel {ch_id}"))
                channel_links_html.append(f"▫️ القناة <a href='{link}'>{ch_name_safe}</a>")

        if not channel_links_html:
            self.logger.warning(f"No valid links to build message for user {user_data.get('telegram_id')}")
            return None

        return (
                f"📬 مرحباً {user_identifier_safe}،\n\n"
                f"تمت إضافة قنوات جديدة إلى اشتراكك في \"<b>{subscription_name_safe}</b>\":\n\n" +
                "\n".join(channel_links_html) +
                "\n\n💡 هذه الروابط مخصصة لك ومتاحة لفترة محدودة فقط."
        )