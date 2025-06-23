# services/messaging_service.py

# --- استيرادات أساسية ---
import asyncio
import logging
import uuid
import json
import html
import re  # <-- إضافة جديدة
from datetime import datetime, timezone
from collections import Counter
from dataclasses import asdict
from typing import Dict, List, Optional, Any
from database.db_queries import add_scheduled_task
# --- استيرادات من مشروعك ---
from utils.messaging_batch import BatchStatus, BatchType, MessagingBatchResult, FailedSendDetail
from utils.db_utils import generate_shared_invite_link_for_channel, send_message_to_user

# --- استيرادات من مكتبة aiogram ---
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramAPIError
)


# --- دوال مساعدة معيارية ---

async def sanitize_for_html(text: Optional[str]) -> str:
    """يهرب الحروف الخاصة في النص ليكون آمنًا للاستخدام داخل HTML."""
    if text is None:
        return ""
    return html.escape(str(text))


def classify_and_translate_error(error: Any) -> tuple[str, str]:
    """
    يصنف الخطأ ويعيد مفتاحًا معياريًا ورسالة مترجمة.
    """
    error_str = str(error).lower()
    if "bot was blocked by the user" in error_str:
        return "user_blocked", "المستخدم قام بحظر البوت."
    if "chat not found" in error_str:
        return "chat_not_found", "المحادثة غير موجودة (قد يكون المستخدم حذف حسابه)."
    if "user is deactivated" in error_str:
        return "user_deactivated", "حساب المستخدم معطل."
    if "can't parse entities" in error_str:
        return "parse_error", "خطأ في تنسيق الرسالة (HTML/Markdown)."
    # يمكنك إضافة المزيد من الحالات هنا
    return "unknown_error", f"خطأ غير معروف: {str(error)}"


# --- كلاس خدمة الرسائل الرئيسي ---

class BackgroundMessagingService:
    def __init__(self, db_pool, telegram_bot):
        self.db_pool = db_pool
        self.telegram_bot = telegram_bot
        self.logger = logging.getLogger(__name__)
        self.SEND_BATCH_SIZE = 25
        self.SEND_BATCH_DELAY_SECONDS = 1

    async def _update_batch_progress(self, batch_id: str, successful_count: int, failed_count: int):
        """
        تحديث دوري لعدادات التقدم في مهمة معينة.
        لا تحدث الحالة النهائية، فقط العدادات.
        """
        try:
            async with self.db_pool.acquire() as connection:
                await connection.execute("""
                    UPDATE messaging_batches
                    SET successful_sends = successful_sends + $1,
                        failed_sends = failed_sends + $2
                    WHERE batch_id = $3
                """, successful_count, failed_count, batch_id)
            self.logger.debug(
                f"Progress updated for batch {batch_id}: +{successful_count} success, +{failed_count} failed.")
        except Exception as e:
            self.logger.error(f"Failed to periodically update progress for batch {batch_id}: {e}", exc_info=True)

    async def start_channel_removal_scheduling_batch(
            self,
            subscription_type_id: int,
            channels_to_schedule: List[Dict]
    ) -> str:
        """
        يبدأ مهمة خلفية لجدولة إزالة المستخدمين من القنوات عند انتهاء اشتراكهم.
        """
        async with self.db_pool.acquire() as conn:
            # جلب المستخدمين الذين يحتاجون إلى جدولة
            query = """
               SELECT u.telegram_id, u.full_name, u.username, s.expiry_date
               FROM subscriptions s
               JOIN users u ON s.telegram_id = u.telegram_id
               WHERE s.subscription_type_id = $1 AND s.is_active = TRUE AND s.expiry_date > NOW()
            """
            target_users_records = await conn.fetch(query, subscription_type_id)

        if not target_users_records:
            self.logger.warning(f"No active subscribers found for type {subscription_type_id} to schedule removals.")
            raise ValueError("No users to schedule removals for.")

        target_users = [dict(r) for r in target_users_records]
        batch_id = str(uuid.uuid4())
        context_data = {
            "channels_to_schedule": channels_to_schedule
        }

        await self._create_batch_record(
            batch_id=batch_id,
            batch_type=BatchType.SCHEDULE_REMOVAL,
            total_users=len(target_users),
            subscription_type_id=subscription_type_id,
            context_data=context_data
        )

        asyncio.create_task(self._process_batch(
            batch_id, BatchType.SCHEDULE_REMOVAL, target_users, context_data=context_data
        ))
        return batch_id

    # =========================================================================
    # ===   1. دوال بدء المهام (Batch Starters)
    # =========================================================================

    async def start_invite_batch(
            self,
            subscription_type_id: int,
            newly_added_channels: List[Dict],
            subscription_type_name: str
    ) -> str:
        """
        يبدأ مهمة إرسال دعوات لمشتركين نشطين عند إضافة قنوات جديدة.
        """
        async with self.db_pool.acquire() as conn:
            query = """
               SELECT u.telegram_id, u.full_name, u.username
               FROM subscriptions s
               JOIN users u ON s.telegram_id = u.telegram_id
               WHERE s.subscription_type_id = $1 AND s.is_active = TRUE AND s.expiry_date > NOW()
            """
            target_users_records = await conn.fetch(query, subscription_type_id)

        if not target_users_records:
            self.logger.warning(
                f"No active subscribers found for subscription type ID {subscription_type_id}. Invite batch not started.")
            raise ValueError("No users to send invites to.")

        target_users = [dict(r) for r in target_users_records]
        batch_id = str(uuid.uuid4())
        context_data = {
            "channels_to_invite": newly_added_channels,
            "subscription_type_name": subscription_type_name
        }

        await self._create_batch_record(
            batch_id=batch_id,
            batch_type=BatchType.INVITE,
            total_users=len(target_users),
            subscription_type_id=subscription_type_id,
            context_data=context_data
        )

        asyncio.create_task(self._process_batch(
            batch_id, BatchType.INVITE, target_users, context_data=context_data
        ))
        return batch_id

    # <-- تعديل كبير: هذه هي دالة البث الجديدة والمحسّنة
    async def start_enhanced_broadcast_batch(
            self,
            message_text: str,
            target_group: str,
            subscription_type_id: Optional[int] = None
    ) -> str:
        """
        يبدأ مهمة بث محسنة مع استهداف مرن ودعم للمتغيرات.
        """
        async with self.db_pool.acquire() as connection:
            target_users_records = await self._get_target_users_for_group(
                connection, target_group, subscription_type_id
            )

        if not target_users_records:
            self.logger.warning(f"No users found for target group '{target_group}'. Broadcast batch not started.")
            raise ValueError("No users found for the specified target group.")

        target_users = [dict(user_record) for user_record in target_users_records]
        batch_id = str(uuid.uuid4())
        message_content = {"text": message_text}
        context_data = {"target_group": target_group}

        await self._create_batch_record(
            batch_id=batch_id,
            batch_type=BatchType.BROADCAST,
            total_users=len(target_users),
            target_group=target_group,  # <-- حقل جديد
            subscription_type_id=subscription_type_id,
            message_content=message_content,
            context_data=context_data
        )

        asyncio.create_task(
            self._process_batch(
                batch_id,
                BatchType.BROADCAST,
                target_users,
                message_content=message_content,
                context_data=context_data
            )
        )
        return batch_id

    # =========================================================================
    # ===   2. المحرك الرئيسي للمعالجة في الخلفية (_process_batch)
    # =========================================================================

    async def _process_batch(
            self,
            batch_id: str,
            batch_type: BatchType,
            users: List[Dict],
            context_data: Optional[Dict] = None,
            message_content: Optional[Dict] = None
    ):
        self.logger.info(f"Starting processing batch {batch_id} ({batch_type.value}) for {len(users)} users.")
        await self._update_batch_status(batch_id, BatchStatus.IN_PROGRESS, started_at=datetime.now(timezone.utc))

        # عدادات إجمالية لتتبع التقدم الكلي
        total_successful_sends = 0
        total_failed_sends = 0

        # عدادات مؤقتة للدفعة الحالية (تستخدم فقط في حالات الإرسال الفعلي)
        current_batch_successful = 0
        current_batch_failed = 0

        send_errors: List[FailedSendDetail] = []

        # تحضير البيانات المسبقة خارج الحلقة الرئيسية لتجنب التكرار
        original_broadcast_text = None
        prepared_invite_data = {}

        if batch_type == BatchType.BROADCAST and message_content:
            original_broadcast_text = message_content.get("text")

        elif batch_type == BatchType.INVITE and context_data:
            links_map = {}
            channels_for_links = context_data.get("channels_to_invite", [])
            subscription_name_for_links = context_data.get('subscription_type_name', 'Your Subscription')

            for ch_info in channels_for_links:
                channel_id = ch_info.get('channel_id')
                channel_name = ch_info.get('channel_name', f"Channel {channel_id}")
                if not channel_id:
                    self.logger.error(f"Missing channel_id in context_data for batch {batch_id}: {ch_info}")
                    continue
                try:
                    res = await generate_shared_invite_link_for_channel(
                        self.telegram_bot, int(channel_id), channel_name,
                        link_name_prefix=f"Invite for {subscription_name_for_links}"
                    )
                    if res and res.get("success") and res.get("invite_link"):
                        links_map[channel_id] = res["invite_link"]
                    else:
                        error_msg = res.get('error', 'Unknown error generating link')
                        self.logger.error(
                            f"Failed to generate invite link for channel {channel_id} ('{channel_name}') in batch {batch_id}: {error_msg}")
                except Exception as e_link:
                    self.logger.error(
                        f"Exception generating invite link for channel {channel_id} in batch {batch_id}: {e_link}",
                        exc_info=True)

            prepared_invite_data["links_map"] = links_map
            if not links_map and channels_for_links:
                self.logger.warning(
                    f"Batch {batch_id}: No invite links were generated, invite messages might be incomplete or fail.")

        # --- الحلقة الرئيسية ---
        for idx, user_data in enumerate(users):

            # *** معالجة حالة جدولة الإزالة الجديدة ***
            if batch_type == BatchType.SCHEDULE_REMOVAL:
                telegram_id = user_data.get('telegram_id')
                expiry_date = user_data.get('expiry_date')
                channels = context_data.get("channels_to_schedule", [])

                if not telegram_id or not expiry_date or not channels:
                    self.logger.error(f"Batch {batch_id}: Missing data for scheduling removal for user: {user_data}")
                    total_failed_sends += 1
                    send_errors.append(FailedSendDetail(
                        telegram_id=telegram_id or 0,
                        error_message="بيانات ناقصة (معرف المستخدم أو تاريخ الانتهاء أو القنوات)", is_retryable=False,
                        full_name=user_data.get('full_name'), username=user_data.get('username'),
                        error_type="DataError", error_key="missing_data"
                    ))
                    continue  # انتقل للمستخدم التالي

                try:
                    # استخدم اتصال واحد لجدولة كل القنوات لهذا المستخدم
                    async with self.db_pool.acquire() as connection:
                        for channel_info in channels:
                            channel_id = channel_info['channel_id']
                            await add_scheduled_task(
                                connection=connection, task_type="remove_user",
                                telegram_id=telegram_id, channel_id=channel_id,
                                execute_at=expiry_date, clean_up=True
                            )
                    total_successful_sends += 1  # نعتبر أن جدولة كل القنوات للمستخدم هي عملية واحدة ناجحة

                except Exception as e_schedule:
                    total_failed_sends += 1
                    error_key, translated_message = classify_and_translate_error(str(e_schedule))
                    send_errors.append(FailedSendDetail(
                        telegram_id=telegram_id, error_message=f"فشل الجدولة: {translated_message}", is_retryable=True,
                        full_name=user_data.get('full_name'), username=user_data.get('username'),
                        error_type=type(e_schedule).__name__, error_key=error_key
                    ))
                    self.logger.error(
                        f"Batch {batch_id}: Failed to schedule removal for user {telegram_id}: {e_schedule}",
                        exc_info=True)

            # *** معالجة الحالات الحالية (البث والدعوات) ***
            else:
                telegram_id = user_data.get('telegram_id')
                if not telegram_id:
                    error_key, translated_message = classify_and_translate_error("missing telegram_id")
                    total_failed_sends += 1
                    current_batch_failed += 1
                    send_errors.append(FailedSendDetail(
                        telegram_id=0, error_message=translated_message, is_retryable=False,
                        full_name=user_data.get('full_name'), username=user_data.get('username'),
                        error_type="DataError", error_key=error_key
                    ))
                    self.logger.error(f"Missing telegram_id for user at index {idx} in batch {batch_id}. Skipping.")

                else:  # في حالة وجود telegram_id، نستمر في محاولة الإرسال
                    message_to_send = None
                    try:
                        # --- بناء محتوى الرسالة ---
                        if batch_type == BatchType.INVITE:
                            if not context_data: raise ValueError("Cannot send invite: context_data is missing.")
                            if not prepared_invite_data.get("links_map") and context_data.get(
                                "channels_to_invite"): raise ValueError(
                                "Cannot send invite: No valid invite links were generated for channels.")
                            message_to_send = await self._build_invite_message(user_data, context_data,
                                                                               prepared_invite_data.get("links_map",
                                                                                                        {}))

                        elif batch_type == BatchType.BROADCAST:
                            if not original_broadcast_text: raise ValueError("Broadcast message text is empty.")
                            message_to_send = self._replace_message_variables(original_broadcast_text, user_data)

                        if not message_to_send: raise ValueError(
                            "Message content is unexpectedly empty before sending.")

                        # --- الإرسال ---
                        await send_message_to_user(self.telegram_bot, telegram_id, message_to_send, parse_mode="HTML")
                        total_successful_sends += 1
                        current_batch_successful += 1

                    # --- معالجة الأخطاء التفصيلية ---
                    except TelegramRetryAfter as e_flood:
                        total_failed_sends += 1
                        current_batch_failed += 1
                        error_key, translated_message = classify_and_translate_error(str(e_flood))
                        send_errors.append(FailedSendDetail(
                            telegram_id=telegram_id, error_message=translated_message, is_retryable=True,
                            full_name=user_data.get('full_name'), username=user_data.get('username'),
                            error_type=type(e_flood).__name__, error_key=error_key
                        ))
                        self.logger.warning(
                            f"Batch {batch_id}: Flood wait for user {telegram_id}. Waiting {e_flood.retry_after}s. {e_flood}")
                        await asyncio.sleep(e_flood.retry_after + 1)

                    except TelegramForbiddenError as e_forbidden:
                        total_failed_sends += 1
                        current_batch_failed += 1
                        error_key, translated_message = classify_and_translate_error(str(e_forbidden))
                        send_errors.append(FailedSendDetail(
                            telegram_id=telegram_id, error_message=translated_message, is_retryable=False,
                            full_name=user_data.get('full_name'), username=user_data.get('username'),
                            error_type=type(e_forbidden).__name__, error_key=error_key
                        ))
                        self.logger.warning(
                            f"Batch {batch_id}: User {telegram_id} cannot be reached (blocked/deactivated/etc.): {e_forbidden}")

                    except TelegramBadRequest as e_bad_request:
                        error_message_original = str(e_bad_request).lower()
                        handled_by_alternative_send = False

                        if "can't parse entities" in error_message_original:
                            self.logger.error(
                                f"Batch {batch_id}: HTML parsing error for user {telegram_id}. Name: '{user_data.get('full_name')}', Username: '{user_data.get('username')}'. Original error: {e_bad_request}")
                            try:
                                alt_message_to_send = None
                                alt_user_data = user_data.copy()
                                alt_user_data['full_name'] = "عزيزي المستخدم"  # اسم بديل وآمن
                                alt_user_data['username'] = None

                                if batch_type == BatchType.INVITE and context_data and "links_map" in prepared_invite_data:
                                    alt_message_to_send = await self._build_invite_message(alt_user_data, context_data,
                                                                                           prepared_invite_data.get(
                                                                                               "links_map", {}))
                                elif batch_type == BatchType.BROADCAST and original_broadcast_text:
                                    alt_message_to_send = self._replace_message_variables(original_broadcast_text,
                                                                                          alt_user_data)

                                if alt_message_to_send:
                                    self.logger.info(
                                        f"Batch {batch_id}: Attempting to send alternative (sanitized name) message to {telegram_id} after parse error.")
                                    await send_message_to_user(self.telegram_bot, telegram_id, alt_message_to_send,
                                                               parse_mode="HTML")
                                    total_successful_sends += 1
                                    current_batch_successful += 1
                                    handled_by_alternative_send = True
                            except Exception as e_alt_send:
                                self.logger.error(
                                    f"Batch {batch_id}: Failed to send alternative message to {telegram_id} after parse error: {e_alt_send}",
                                    exc_info=True)

                        if not handled_by_alternative_send:
                            total_failed_sends += 1
                            current_batch_failed += 1
                            is_retryable_bad_req = not (
                                        "chat not found" in error_message_original or "user is bot" in error_message_original or "can't parse entities" in error_message_original)
                            error_key, translated_message = classify_and_translate_error(str(e_bad_request))
                            send_errors.append(FailedSendDetail(
                                telegram_id=telegram_id, error_message=translated_message,
                                is_retryable=is_retryable_bad_req,
                                full_name=user_data.get('full_name'), username=user_data.get('username'),
                                error_type=type(e_bad_request).__name__, error_key=error_key
                            ))
                            self.logger.error(
                                f"Batch {batch_id}: Bad request sending to {telegram_id} (or failed alt send for parse error): {e_bad_request}")

                    except (TelegramAPIError, ValueError, Exception) as e:
                        total_failed_sends += 1
                        current_batch_failed += 1
                        error_key, translated_message = classify_and_translate_error(str(e))
                        is_retryable = not isinstance(e, ValueError)
                        error_type = type(e).__name__
                        if isinstance(e, ValueError):
                            error_type = "LogicError"
                        elif not isinstance(e, TelegramAPIError):
                            error_type = "UnexpectedError"

                        send_errors.append(FailedSendDetail(
                            telegram_id=telegram_id, error_message=translated_message, is_retryable=is_retryable,
                            full_name=user_data.get('full_name'), username=user_data.get('username'),
                            error_type=error_type, error_key=error_key
                        ))
                        self.logger.error(f"Batch {batch_id}: A {error_type} occurred for user {telegram_id}: {e}",
                                          exc_info=True)

            # التحديث الدوري لقاعدة البيانات والإيقاف المؤقت (ينطبق على جميع أنواع المهام)
            # نتحقق إذا وصلنا إلى نهاية دفعة أو إذا كانت هذه هي آخر رسالة في القائمة
            if (idx + 1) % self.SEND_BATCH_SIZE == 0 or (idx + 1) == len(users):
                # بالنسبة لمهام الجدولة، ستكون هذه العدادات صفراً، لذلك لن يحدث تحديث غير ضروري
                if current_batch_successful > 0 or current_batch_failed > 0:
                    self.logger.info(
                        f"Batch {batch_id}: Committing progress. Success: {current_batch_successful}, Failed: {current_batch_failed}")
                    await self._update_batch_progress(batch_id, current_batch_successful, current_batch_failed)
                    current_batch_successful = 0
                    current_batch_failed = 0

                # إيقاف مؤقت بين الدفعات (وليس بعد الدفعة الأخيرة)
                if (idx + 1) < len(users):
                    self.logger.info(
                        f"Batch {batch_id}: Processed {idx + 1}/{len(users)} users, pausing for {self.SEND_BATCH_DELAY_SECONDS}s.")
                    await asyncio.sleep(self.SEND_BATCH_DELAY_SECONDS)

        # تحديث الحالة النهائية وتفاصيل الأخطاء
        error_summary = dict(Counter(err.error_key for err in send_errors if err.error_key))
        await self._update_final_batch_status(batch_id, total_successful_sends, total_failed_sends, send_errors,
                                              error_summary)

        self.logger.info(
            f"Batch {batch_id} completed. Total Success: {total_successful_sends}, Total Failed: {total_failed_sends}.")
    # =========================================================================
    # ===   3. دوال مساعدة جديدة ومحفوظة (Helpers)
    # =========================================================================

    @staticmethod
    async def _get_target_users_for_group(
            connection, target_group: str, subscription_type_id: Optional[int] = None
    ) -> List[Dict]:
        """دالة لجلب المستخدمين حسب الاستهداف مع كافة البيانات اللازمة للمتغيرات."""
        params = []

        # ✅ --- تعديل: إزالة جميع شروط u.is_active = true من الاستعلامات التالية ---

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

    @staticmethod  # <-- تم التعديل هنا
    def _replace_message_variables(message_text: str, user_data: Dict) -> str:
        """دالة لاستبدال المتغيرات في نص الرسالة."""
        if not message_text: return ""
        processed_text = message_text

        # متغيرات المستخدم
        full_name = user_data.get('full_name') or 'المستخدم'
        first_name = full_name.split()[0]
        username = user_data.get('username')
        telegram_id = str(user_data.get('telegram_id', ''))

        processed_text = processed_text.replace('{FULL_NAME}', html.escape(full_name))
        processed_text = processed_text.replace('{FIRST_NAME}', html.escape(first_name))
        processed_text = processed_text.replace('{USERNAME}', f"@{username}" if username else "")
        processed_text = processed_text.replace('{USER_ID}', telegram_id)

        # متغيرات الاشتراك
        subscription_name = user_data.get('subscription_name') or ''
        expiry_date = user_data.get('expiry_date')

        processed_text = processed_text.replace('{SUBSCRIPTION_NAME}', html.escape(subscription_name))

        if expiry_date:
            formatted_date = expiry_date.strftime('%Y-%m-%d')
            processed_text = processed_text.replace('{EXPIRY_DATE}', formatted_date)

            now_utc = datetime.now(timezone.utc)
            expiry_utc = expiry_date.replace(tzinfo=timezone.utc) if expiry_date.tzinfo is None else expiry_date
            delta = expiry_utc - now_utc

            if delta.days >= 0:
                processed_text = processed_text.replace('{DAYS_REMAINING}', str(delta.days))
                processed_text = processed_text.replace('{DAYS_SINCE_EXPIRY}', '0')
            else:
                processed_text = processed_text.replace('{DAYS_REMAINING}', '0')
                processed_text = processed_text.replace('{DAYS_SINCE_EXPIRY}', str(abs(delta.days)))
        else:
            # إزالة المتغيرات إذا لم تكن البيانات متوفرة
            for key in ['{EXPIRY_DATE}', '{DAYS_REMAINING}', '{DAYS_SINCE_EXPIRY}', '{SUBSCRIPTION_NAME}']:
                processed_text = processed_text.replace(key, '')

        # متغيرات النظام (يمكنك تخصيصها من الإعدادات لاحقًا)

        # إزالة أي متغيرات متبقية لم يتم استبدالها
        processed_text = re.sub(r'\{[A-Z_]+}', '', processed_text)

        return processed_text

    async def _build_invite_message(self, user_data: Dict, context_data: Dict, links_map: Dict) -> Optional[str]:
        """بناء رسالة الدعوة (من كودك الحالي)."""
        if not user_data or not context_data:
            self.logger.error(
                f"Missing critical data for building invite message: user_data is None: {user_data is None}, context_data is None: {context_data is None}")
            return None

        raw_full_name = user_data.get('full_name')
        raw_username = user_data.get('username')
        telegram_id_str = str(user_data.get('telegram_id', 'N/A'))
        identifier_to_use = raw_full_name if raw_full_name else (
            f"@{raw_username}" if raw_username else telegram_id_str)
        user_identifier_safe = await sanitize_for_html(identifier_to_use)
        subscription_name_safe = await sanitize_for_html(
            context_data.get("subscription_type_name", "your subscription"))

        channel_links_to_send = []
        channels_to_invite = context_data.get("channels_to_invite", [])
        if isinstance(channels_to_invite, list):
            for ch_info in channels_to_invite:
                if isinstance(ch_info, dict):
                    ch_id = ch_info.get("channel_id")
                    ch_name_safe = await sanitize_for_html(ch_info.get("channel_name", f"Channel {ch_id}"))
                    link = links_map.get(ch_id)
                    if link:
                        channel_links_to_send.append(f"▫️ القناة <a href='{link}'>{ch_name_safe}</a>")

        if not channel_links_to_send:
            self.logger.warning(
                f"No valid invite links available to build invite message for user {user_identifier_safe} ({user_data.get('telegram_id')}). Links map: {links_map}, Channels to invite: {channels_to_invite}")
            return None

        return (
                f"📬 مرحباً {user_identifier_safe}،\n\n"
                f"تمت إضافة قنوات جديدة إلى اشتراكك في \"<b>{subscription_name_safe}</b>\":\n\n" +
                "\n".join(channel_links_to_send) +
                "\n\n💡 هذه الروابط مخصصة لك ومتاحة لفترة محدودة فقط."
        )

    # =========================================================================
    # ===   4. دوال التفاعل مع قاعدة البيانات (Database Interaction)
    # =========================================================================

    async def _create_batch_record(self, batch_id: str, batch_type: BatchType, total_users: int, **kwargs):
        """تحديث الدالة لتشمل حقل `target_group` الجديد."""
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
        """جلب حالة مهمة معينة (من كودك الحالي)."""
        async with self.db_pool.acquire() as connection:
            record = await connection.fetchrow(
                """
                SELECT 
                    batch_id, batch_type, status, total_users, successful_sends,
                    failed_sends, created_at, started_at, completed_at, 
                    subscription_type_id, message_content, context_data,
                    error_details, error_summary, target_group
                FROM messaging_batches 
                WHERE batch_id = $1
                """,
                batch_id
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

    async def retry_failed_sends_in_batch(self, original_batch_id: str) -> str:
        """إعادة محاولة الإرسال للفاشلين في مهمة (من كودك الحالي)."""
        self.logger.info(f"Attempting to retry failed sends for batch {original_batch_id}")
        original_batch = await self.get_batch_status(original_batch_id)

        if not original_batch:
            raise ValueError(f"Batch with ID {original_batch_id} not found.")

        failed_sends_to_retry = [
            detail for detail in original_batch.error_details
            if detail.telegram_id and detail.is_retryable
        ]

        if not failed_sends_to_retry:
            raise ValueError("No retryable failed sends found in this batch to retry.")

        new_batch_id = str(uuid.uuid4())

        await self._create_batch_record(
            batch_id=new_batch_id,
            batch_type=original_batch.batch_type,
            total_users=len(failed_sends_to_retry),
            subscription_type_id=original_batch.subscription_type_id,
            message_content=original_batch.message_content,
            context_data=original_batch.context_data,
            target_group=original_batch.target_group  # تمرير المجموعة المستهدفة للمهمة الجديدة
        )

        users_for_retry_task = [
            {'telegram_id': fs.telegram_id, 'full_name': fs.full_name, 'username': fs.username}
            for fs in failed_sends_to_retry
        ]

        asyncio.create_task(self._process_batch(
            batch_id=new_batch_id,
            batch_type=original_batch.batch_type,
            users=users_for_retry_task,
            context_data=original_batch.context_data,
            message_content=original_batch.message_content
        ))

        self.logger.info(
            f"Started new retry batch {new_batch_id} for {len(users_for_retry_task)} users from original batch {original_batch_id}.")
        return new_batch_id

    async def _update_batch_status(self, batch_id: str, status: BatchStatus, started_at: Optional[datetime] = None):
        """تحديث حالة المهمة (من كودك الحالي)."""
        async with self.db_pool.acquire() as connection:
            if status == BatchStatus.IN_PROGRESS and started_at:
                await connection.execute("""
                    UPDATE messaging_batches SET status = $1, started_at = $2
                    WHERE batch_id = $3 AND started_at IS NULL
                """, status.value, started_at, batch_id)
            else:
                await connection.execute("UPDATE messaging_batches SET status = $1 WHERE batch_id = $2", status.value,
                                         batch_id)

    async def _update_final_batch_status(
            self,
            batch_id: str,
            total_successful: int,
            total_failed: int,
            error_details: List[FailedSendDetail],
            error_summary: Dict[str, int]
    ):
        """
        تحديث الحالة النهائية للمهمة وتفاصيل الأخطاء بعد اكتمالها.
        """
        if total_successful == 0 and total_failed > 0:
            final_status = BatchStatus.FAILED
        else:
            final_status = BatchStatus.COMPLETED

        error_details_for_db = [asdict(detail) for detail in error_details]
        error_summary_json = json.dumps(error_summary) if error_summary else None

        async with self.db_pool.acquire() as connection:
            await connection.execute("""
                   UPDATE messaging_batches
                   SET status = $1, 
                       error_details = $2, 
                       completed_at = $3, 
                       error_summary = $4
                   WHERE batch_id = $5
               """,
                                     final_status.value,
                                     json.dumps(error_details_for_db) if error_details_for_db else None,
                                     datetime.now(timezone.utc),
                                     error_summary_json,
                                     batch_id
                                     )