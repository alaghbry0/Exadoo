#  /utils/discount_utils.py

from decimal import Decimal


def calculate_discounted_price(base_price: Decimal, discount_type: str, discount_value: Decimal) -> Decimal:
    """
    Calculates the final price after applying a discount.
    This is the single source of truth for all discount calculations.
    """
    # ضمان أن المدخلات من النوع الصحيح
    base_price = Decimal(base_price)
    discount_value = Decimal(discount_value)

    if discount_type == 'percentage':
        # تأكد أن الخصم لا يتجاوز 100%
        if discount_value > 100:
            discount_value = Decimal('100')

        discount_multiplier = Decimal('1') - (discount_value / Decimal('100'))
        final_price = base_price * discount_multiplier

    elif discount_type == 'fixed_amount':
        # تأكد أن السعر لا يصبح سالباً
        final_price = max(Decimal('0.00'), base_price - discount_value)

    else:
        # إذا كان نوع الخصم غير معروف، لا تطبق أي خصم
        final_price = base_price

    # قم بتقريب السعر النهائي إلى منزلتين عشريتين (ممارسة جيدة للتعاملات المالية)
    return final_price.quantize(Decimal('0.01'))