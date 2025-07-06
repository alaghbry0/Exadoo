# utils/system_notifications.py

import logging
import html  # <-- 1. إضافة استيراد جديد ومهم
from aiogram import Bot
from asyncpg import Pool
from datetime import datetime
import pytz
from typing import Literal

from utils.db_utils import send_message_to_user

# إعدادات
LEVEL_EMOJI = {
    "CRITICAL": "🚨",
    "ERROR": "❌",
    "WARNING": "⚠️",
    "INFO": "ℹ️",
}
LOCAL_TZ = pytz.timezone("Asia/Riyadh")


async def send_system_notification(
    db_pool: Pool,
    bot: Bot,
    level: Literal["CRITICAL", "ERROR", "WARNING", "INFO"],
    audience: Literal["developer", "admin", "all"],
    title: str,
    details: dict[str, str]
):
    """
    يرسل إشعار نظام مخصص إلى الجمهور المستهدف.
    """
    emoji = LEVEL_EMOJI.get(level.upper(), "ℹ️")
    timestamp = datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')

    audience_map = {
        "developer": "إشعار للمطور",
        "admin": "إشعار للإدارة",
        "all": "إشعار للجميع"
    }
    audience_text = audience_map.get(audience, "إشعار للنظام")

    # === 2. التعديل الرئيسي هنا ===
    # استبدال `<code>` بـ `<i>` وإضافة `html.escape` للأمان
    details_text = "\n".join(
        [f"▪️ <b>{key}:</b> <i>{html.escape(str(value))}</i>" for key, value in details.items() if value]
    )

    formatted_message = (
        f"{emoji} <b>{audience_text}: {title}</b>\n\n"
        f"<b><u>التفاصيل:</u></b>\n"
        f"{details_text}\n\n"
        f"<i>توقيت الخادم: {timestamp}</i>"
    )

    recipients = []
    try:
        async with db_pool.acquire() as conn:
            if audience == 'developer':
                query = """
                    SELECT u.telegram_id FROM panel_users u
                    JOIN roles r ON u.role_id = r.id
                    WHERE u.receives_notifications = TRUE AND u.telegram_id IS NOT NULL
                    AND r.name IN ('owner', 'developer')
                """
            elif audience == 'admin':
                query = """
                    SELECT u.telegram_id FROM panel_users u
                    JOIN roles r ON u.role_id = r.id
                    WHERE u.receives_notifications = TRUE AND u.telegram_id IS NOT NULL
                    AND r.name IN ('owner', 'admin')
                """
            else:
                query = """
                    SELECT telegram_id FROM panel_users
                    WHERE receives_notifications = TRUE AND telegram_id IS NOT NULL
                """
            rows = await conn.fetch(query)
            recipients = [row['telegram_id'] for row in rows]
    except Exception as e:
        logging.error(f"[SystemNotify] فشل في جلب مستلمي الإشعارات: {e}", exc_info=True)
        return

    if not recipients:
        logging.warning(f"[SystemNotify] لم يتم العثور على مستلمين للإشعار من نوع '{audience}'.")
        return

    unique_recipients = set(recipients)
    logging.info(f"[SystemNotify] إرسال إشعار '{title}' إلى {len(unique_recipients)} مستلم.")

    for telegram_id in unique_recipients:
        try:
            await send_message_to_user(bot, telegram_id, formatted_message, parse_mode="HTML")
        except Exception as e:
            logging.error(f"[SystemNotify] فشل في إرسال الإشعار إلى المسؤول {telegram_id}: {e}")