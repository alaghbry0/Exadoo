import asyncio
import logging
import uuid
import json
from typing import Dict, Any, List, Set

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from services.background_tasks.base_handler import BaseTaskHandler


class ChannelAuditHandler(BaseTaskHandler):
    """
    معالج متخصص لتنفيذ فحص القنوات، تحديد المشتركين غير النشطين، وحفظ الإحصائيات.
    """

    def __init__(self, db_pool, telegram_bot):
        super().__init__(db_pool, telegram_bot)
        # تأخير بسيط بين كل فحص لمستخدم لتجنب الضغط على API تيليجرام
        self.CHECK_DELAY = 0.05  # 50 ميلي ثانية

    async def prepare_for_batch(self, context_data: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
        """
        يتم استدعاؤه مرة واحدة في بداية المهمة.
        هنا، سنقوم بجلب كل المستخدمين والمشتركين النشطين من قاعدة البيانات.
        """
        self.logger.info(f"[{batch_id}] Preparing data for channel audit.")
        audit_uuid_str = context_data.get("audit_uuid")
        if not audit_uuid_str:
            raise ValueError("Audit UUID is required for channel audit task.")

        async with self.db_pool.acquire() as conn:
            # 1. جلب كل المستخدمين المسجلين في قاعدة بياناتنا
            all_db_users_records = await conn.fetch("SELECT telegram_id FROM users")
            all_db_user_ids = {rec['telegram_id'] for rec in all_db_users_records}

            # 2. جلب كل المشتركين النشطين في جميع القنوات
            active_subs_records = await conn.fetch(
                "SELECT telegram_id, channel_id FROM subscriptions WHERE is_active = TRUE AND expiry_date > NOW()"
            )
            # تنظيمهم في قاموس لسهولة الوصول: {channel_id: {user_id1, user_id2}}
            active_subs_by_channel: Dict[int, Set[int]] = {}
            for sub in active_subs_records:
                channel_id = sub['channel_id']
                if channel_id not in active_subs_by_channel:
                    active_subs_by_channel[channel_id] = set()
                active_subs_by_channel[channel_id].add(sub['telegram_id'])

        return {
            "audit_uuid": uuid.UUID(audit_uuid_str),
            "all_db_user_ids": all_db_user_ids,
            "active_subs_by_channel": active_subs_by_channel
        }

    async def process_item(self, channel_data: Dict[str, Any], context_data: Dict[str, Any],
                           prepared_data: Dict[str, Any]) -> None:
        channel_id = channel_data['channel_id']
        channel_name = channel_data.get('channel_name', f"Channel {channel_id}")
        audit_uuid = prepared_data['audit_uuid']

        self.logger.info(f"[{audit_uuid}] Auditing channel: {channel_name} ({channel_id})")

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO channel_audits (audit_uuid, channel_id, channel_name, status)
                VALUES ($1, $2, $3, 'RUNNING') ON CONFLICT (audit_uuid, channel_id) DO NOTHING
                """,
                audit_uuid, channel_id, channel_name
            )

        try:
            total_members_api = await self.bot.get_chat_member_count(channel_id)
            # --- ✅ تحسين 1: تحديث مبكر لعدد الأعضاء ---
            await self._update_partial_audit_results(audit_uuid, channel_id, {'total_members_api': total_members_api})

        except Exception as e:
            self.logger.error(f"[{audit_uuid}] Failed to get member count for {channel_id}: {e}")
            await self._update_audit_status(audit_uuid, channel_id, 'FAILED', error_message=str(e))
            raise e

        active_subs_in_this_channel = prepared_data['active_subs_by_channel'].get(channel_id, set())
        active_subscribers_db_count = len(active_subs_in_this_channel)
        # --- ✅ تحسين 2: تحديث مبكر لعدد المشتركين النشطين ---
        await self._update_partial_audit_results(audit_uuid, channel_id,
                                                 {'active_subscribers_db': active_subscribers_db_count})

        all_db_user_ids = prepared_data['all_db_user_ids']
        users_to_check = all_db_user_ids - active_subs_in_this_channel

        inactive_users_found_ids = []

        # --- ✅ تحسين 3: تسجيل التقدم داخل الحلقة الطويلة ---
        total_to_check = len(users_to_check)
        for i, user_id in enumerate(users_to_check):
            try:
                member = await self.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                    inactive_users_found_ids.append(user_id)
            # ... (باقي معالجة الأخطاء) ...
            except Exception as e:
                self.logger.error(f"[{audit_uuid}] Unexpected error checking user {user_id} in {channel_id}: {e}")

            await asyncio.sleep(self.CHECK_DELAY)

            # تحديث دوري كل 100 مستخدم أو في النهاية
            if (i + 1) % 100 == 0 or (i + 1) == total_to_check:
                self.logger.info(
                    f"[{audit_uuid}] Channel {channel_id}: Checked {i + 1}/{total_to_check} users. Found {len(inactive_users_found_ids)} inactive members so far.")
                await self._update_partial_audit_results(audit_uuid, channel_id,
                                                         {'inactive_in_channel_db': len(inactive_users_found_ids)})

        inactive_in_channel_db_count = len(inactive_users_found_ids)
        unidentified_members_count = total_members_api - active_subscribers_db_count - inactive_in_channel_db_count

        # تحديث السجل بالنتائج النهائية
        await self._update_final_audit_results(
            audit_uuid=audit_uuid,
            channel_id=channel_id,
            total_members_api=total_members_api,
            active_subscribers_db=active_subscribers_db_count,
            inactive_in_channel_db=inactive_in_channel_db_count,
            unidentified_members=max(0, unidentified_members_count),
            users_to_remove_ids=inactive_users_found_ids
        )
        self.logger.info(f"[{audit_uuid}] Completed audit for channel: {channel_name}.")



    async def _update_audit_status(self, audit_uuid: uuid.UUID, channel_id: int, status: str,
                                   error_message: str = None):
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE channel_audits SET status = $1, error_message = $2, completed_at = NOW() WHERE audit_uuid = $3 AND channel_id = $4",
                status, error_message, audit_uuid, channel_id
            )

    async def _update_final_audit_results(self, *, audit_uuid: uuid.UUID, channel_id: int, total_members_api: int,
                                          active_subscribers_db: int, inactive_in_channel_db: int,
                                          unidentified_members: int, users_to_remove_ids: List[int]):
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE channel_audits
                SET status = 'COMPLETED',
                    total_members_api = $1,
                    active_subscribers_db = $2,
                    inactive_in_channel_db = $3,
                    unidentified_members = $4,
                    users_to_remove = $5,
                    completed_at = NOW()
                WHERE audit_uuid = $6 AND channel_id = $7
                """,
                total_members_api,
                active_subscribers_db,
                inactive_in_channel_db,
                unidentified_members,
                json.dumps({"ids": users_to_remove_ids}),
                audit_uuid,
                channel_id
            )

# --- ✅ دالة جديدة للتحديثات الجزئية ---
    async def _update_partial_audit_results(self, audit_uuid: uuid.UUID, channel_id: int, updates: Dict[str, Any]):
        """تحديث أعمدة محددة في سجل الفحص دون تغيير الحالة."""
        if not updates:
            return

        columns_to_update = ", ".join(f"{key} = ${i + 3}" for i, key in enumerate(updates.keys()))
        query = f"UPDATE channel_audits SET {columns_to_update} WHERE audit_uuid = $1 AND channel_id = $2"

        async with self.db_pool.acquire() as conn:
            await conn.execute(query, audit_uuid, channel_id, *updates.values())