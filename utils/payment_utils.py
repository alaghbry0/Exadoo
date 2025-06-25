#utils/payment_utils.py
from decimal import Decimal
from pytoniq import Address
from typing import Optional

OP_JETTON_TRANSFER_NOTIFICATION = 0x7362d09c
OP_JETTON_TRANSFER = 0xf8a7ea5
JETTON_DECIMALS = 6


def normalize_address(addr_str: str) -> Optional[str]:
    """
    دالة مساعدة لتوحيد تنسيق عناوين TON.
    ترجع العنوان بصيغة non-bounceable أو None إذا كان غير صالح.
    """
    try:
        # لا حاجة لإزالة "0:" لأن مكتبة pytoniq تتعامل معها
        addr = Address(addr_str)
        return addr.to_str(is_user_friendly=True, is_bounceable=False, is_url_safe=True)
    except Exception:
        logging.warning(f"❌ فشل في تطبيع العنوان: {addr_str}")
        return None




def convert_amount(raw_value: int, decimals: int) -> Decimal:
    """
    تحويل القيمة الأولية (بالوحدات الصغرى) إلى Decimal بناءً على عدد الخانات العشرية.
    """
    if raw_value is None:
        return Decimal('0')
    return Decimal(raw_value) / (10 ** decimals)
