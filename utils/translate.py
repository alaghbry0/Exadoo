# =============== utils/translate.py (النسخة المحسنة) ===============
import re
from typing import Optional, Tuple

# سنحول القاموس إلى هيكل أكثر تنظيمًا
# المفتاح هو جزء من رسالة الخطأ الأصلية
# القيمة هي tuple: (مفتاح_خطأ_معياري, "الترجمة العربية")
TELEGRAM_ERROR_MAP = {
    # TelegramForbiddenError
    "bot was blocked by the user": ("bot_blocked", "قام المستخدم بحظر البوت."),
    "user is deactivated": ("user_deactivated", "حساب المستخدم معطل."),
    "bot can't initiate conversation with a user": (
    "chat_not_found", "لا يمكن للبوت بدء محادثة (يجب على المستخدم البدء أولاً)."),
    "user is restricted": ("user_restricted", "المستخدم مقيد ولا يمكن استقبال رسائل."),

    # TelegramBadRequest
    "chat not found": ("chat_not_found", "لم يتم العثور على الدردشة (قد يكون المستخدم لم يتفاعل مع البوت)."),
    "peer id invalid": ("peer_id_invalid", "معرف المستخدم أو الدردشة غير صالح."),
    "user_id_invalid": ("user_id_invalid", "معرف المستخدم غير صالح."),
    "can't parse entities": ("parsing_error", "فشل في تحليل تنسيق الرسالة (HTML غير صالح)."),

    # FloodWait
    "flood wait": ("flood_wait", "تم تجاوز حد الطلبات."),

    # أخطاء منطقية داخلية
    "invite message could not be built": ("logic_error_invite", "تعذر إنشاء رسالة الدعوة (لا توجد روابط صالحة)."),
    "broadcast message text is empty": ("logic_error_broadcast", "نص رسالة البث فارغ."),
    "missing telegram_id": ("logic_error_data", "معرف تيليجرام مفقود في بيانات المستخدم."),
}


def classify_and_translate_error(original_message: str) -> Tuple[str, str]:
    """
    يصنف رسالة الخطأ ويعيد مفتاحًا معياريًا وترجمة عربية.

    :param original_message: رسالة الخطأ الأصلية من Telegram API أو النظام.
    :return: Tuple containing (error_key, translated_message).
    """
    if not original_message:
        return "unknown_error", "حدث خطأ غير معروف."

    msg_lower = original_message.lower()

    for key_fragment, (error_key, translation) in TELEGRAM_ERROR_MAP.items():
        if key_fragment in msg_lower:
            if error_key == "flood_wait":
                match = re.search(r"retry after (\d+)", msg_lower)
                if match:
                    wait_seconds = match.group(1)
                    return error_key, f"{translation} انتظر لمدة {wait_seconds} ثانية."
            return error_key, translation

    # إذا لم يتم العثور على تطابق، أعد مفتاحًا عامًا والرسالة الأصلية
    return "generic_api_error", f"فشل الإرسال: {original_message}"