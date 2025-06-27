# =============== utils/messaging_batch.py ===============

from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# --- تعريفات الأنواع والحالات ---

class BatchStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchType(Enum):
    INVITE = "invite"
    BROADCAST = "broadcast"
    SCHEDULE_REMOVAL = "schedule_removal"
    CHANNEL_CLEANUP = "channel_cleanup"
    CHANNEL_AUDIT = "channel_audit"


# --- كلاسات هياكل البيانات (Data Structures) ---

@dataclass
class FailedSendDetail:
    """يمثل تفاصيل محاولة إرسال فاشلة واحدة لمستخدم معين."""
    telegram_id: int
    error_message: str      # الرسالة المترجمة للعرض
    is_retryable: bool
    error_type: str         # اسم كلاس الاستثناء مثل "TelegramForbiddenError"
    error_key: str          # المفتاح المعياري مثل "user_blocked"
    full_name: Optional[str] = None
    username: Optional[str] = None


@dataclass
class MessagingBatchResult:
    """يمثل الحالة الكاملة لدفعة رسائل معينة عند استرجاعها من قاعدة البيانات."""
    batch_id: str
    batch_type: BatchType
    total_users: int
    successful_sends: int
    failed_sends: int
    status: BatchStatus
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_details: List[FailedSendDetail] = field(default_factory=list)
    error_summary: Dict[str, int] = field(default_factory=dict)

    # ### <-- تم التعديل هنا ###
    # نقل الحقول الاختيارية إلى داخل تعريف الكلاس
    subscription_type_id: Optional[int] = None
    message_content: Optional[Dict] = None
    context_data: Optional[Dict] = None
    target_group: Optional[str] = None # إضافة الحقل الجديد