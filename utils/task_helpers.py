# utils/task_helpers.py

import html
import re
from datetime import datetime, timezone
from typing import Optional, Any, Dict


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
    if "missing telegram_id" in error_str:
        return "missing_data", "بيانات ناقصة (معرف المستخدم مفقود)."
    # يمكنك إضافة المزيد من الحالات هنا
    return "unknown_error", f"خطأ غير معروف: {str(error)}"


def sanitize_for_html(text: Optional[str]) -> str:
    """يهرب الحروف الخاصة في النص ليكون آمنًا للاستخدام داخل HTML."""
    if text is None:
        return ""
    return html.escape(str(text))


def replace_message_variables(message_text: str, user_data: Dict) -> str:
    """دالة لاستبدال المتغيرات في نص الرسالة."""
    if not message_text: return ""
    processed_text = message_text

    # متغيرات المستخدم
    full_name = user_data.get('full_name') or 'المستخدم'
    first_name = full_name.split()[0]
    username = user_data.get('username')
    telegram_id = str(user_data.get('telegram_id', ''))

    processed_text = processed_text.replace('{FULL_NAME}', sanitize_for_html(full_name))
    processed_text = processed_text.replace('{FIRST_NAME}', sanitize_for_html(first_name))
    processed_text = processed_text.replace('{USERNAME}', f"@{username}" if username else "")
    processed_text = processed_text.replace('{USER_ID}', telegram_id)

    # متغيرات الاشتراك
    subscription_name = user_data.get('subscription_name') or ''
    expiry_date = user_data.get('expiry_date')

    processed_text = processed_text.replace('{SUBSCRIPTION_NAME}', sanitize_for_html(subscription_name))

    if expiry_date:
        # تأكد من أن expiry_date هو كائن datetime
        if isinstance(expiry_date, str):
            try:
                # محاولة تحويل الصيغة الشائعة
                expiry_date = datetime.fromisoformat(expiry_date)
            except ValueError:
                expiry_date = None  # فشل التحويل

        if expiry_date:
            formatted_date = expiry_date.strftime('%Y-%m-%d')
            processed_text = processed_text.replace('{EXPIRY_DATE}', formatted_date)

            now_utc = datetime.now(timezone.utc)
            expiry_utc = expiry_date.replace(tzinfo=timezone.utc) if expiry_date.tzinfo is None else expiry_date
            delta = expiry_utc - now_utc

            if delta.total_seconds() >= 0:
                processed_text = processed_text.replace('{DAYS_REMAINING}', str(delta.days))
                processed_text = processed_text.replace('{DAYS_SINCE_EXPIRY}', '0')
            else:
                processed_text = processed_text.replace('{DAYS_REMAINING}', '0')
                processed_text = processed_text.replace('{DAYS_SINCE_EXPIRY}', str(abs(delta.days)))

    # إزالة المتغيرات إذا لم تكن البيانات متوفرة
    keys_to_remove = [
        '{EXPIRY_DATE}', '{DAYS_REMAINING}', '{DAYS_SINCE_EXPIRY}', '{SUBSCRIPTION_NAME}'
    ]
    for key in keys_to_remove:
        if key in processed_text and key not in ['{EXPIRY_DATE}', '{DAYS_REMAINING}',
                                                 '{DAYS_SINCE_EXPIRY}'] or not expiry_date:
            processed_text = processed_text.replace(key, '')

    # إزالة أي متغيرات متبقية لم يتم استبدالها لضمان عدم ظهورها للمستخدم
    processed_text = re.sub(r'\{[A-Z_]+}', '', processed_text)

    return processed_text