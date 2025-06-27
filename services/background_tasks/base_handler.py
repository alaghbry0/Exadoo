# services/background_tasks/base_handler.py

from abc import ABC, abstractmethod
from typing import Dict, Any
import logging

class BaseTaskHandler(ABC):
    """
    واجهة أساسية لكل معالجات المهام في الخلفية.
    كل معالج متخصص (بث، دعوة، ...) يجب أن يرث من هذا الكلاس.
    """
    def __init__(self, db_pool, telegram_bot):
        self.db_pool = db_pool
        self.bot = telegram_bot
        self.logger = logging.getLogger(self.__class__.__name__)

    async def prepare_for_batch(self, context_data: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
        """
        دالة اختيارية لتنفيذ أي تحضيرات مسبقة قبل بدء معالجة المستخدمين.
        مثال: إنشاء جميع روابط الدعوة مرة واحدة.
        تعيد قاموسًا من البيانات المحضرة لتمريرها إلى `process_item`.
        """
        return {}

    @abstractmethod
    async def process_item(self, user_data: Dict[str, Any], context_data: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
        """
        الدالة الأساسية التي تعالج عنصراً واحداً (مستخدم).
        يجب أن تثير استثناء (raise exception) في حالة الفشل.
        """
        pass