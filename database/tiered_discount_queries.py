# database/tiered_discount_queries.py

import logging
from decimal import Decimal
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta


# ⭐ تعديل: الدالة أصبحت أكثر مرونة وتقبل البيانات الجديدة (نسخة مدمجة)
async def create_tiered_discount(conn, discount_data: dict, tiers_data: List[dict]) -> dict:
    """
    إنشاء خصم متدرج جديد مع مستوياته بناءً على الهيكل المدمج.
    """
    try:
        # 1. إنشاء الخصم الرئيسي
        discount_query = """
            INSERT INTO discounts (
                name, description, discount_type, discount_value,
                applicable_to_subscription_type_id, applicable_to_subscription_plan_id,
                start_date, end_date, is_active, 
                lock_in_price, -- ⭐ أصبح يُقرأ من البيانات المدخلة
                lose_on_lapse, target_audience, 
                is_tiered, -- ⭐ تمييز الخصم كمتدرج
                price_lock_duration_months -- ⭐ إضافة مدة تثبيت السعر الاختيارية
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, true, $13)
            RETURNING *;
        """
        discount = await conn.fetchrow(
            discount_query,
            discount_data["name"],
            discount_data.get("description"),
            'percentage',  # النوع هنا ليس مهماً جداً لأن القيمة تأتي من المستوى
            Decimal("0"),  # قيمة وهمية، المهم المستويات
            discount_data.get("applicable_to_subscription_type_id"),
            discount_data.get("applicable_to_subscription_plan_id"),
            discount_data.get("start_date"),
            discount_data.get("end_date"),
            discount_data.get("is_active", True),
            discount_data.get("lock_in_price", False),  # ⭐ مرونة كاملة
            discount_data.get("lose_on_lapse", False),
            discount_data["target_audience"],
            discount_data.get("price_lock_duration_months")
        )
        discount_id = discount['id']

        # 2. إنشاء المستويات مع الحقول الجديدة
        tier_query = """
            INSERT INTO discount_tiers (
                discount_id, tier_order, discount_value, max_slots,
                display_fake_count, fake_count_value
            ) VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *;
        """
        created_tiers = []
        # تفعيل المستوى الأول فقط بشكل افتراضي
        is_first_tier = True
        for tier in tiers_data:
            # التحقق من وجود الحقول الإلزامية في كل مستوى
            if not all(k in tier for k in ['tier_order', 'discount_value', 'max_slots']):
                raise ValueError("Each tier must contain 'tier_order', 'discount_value', and 'max_slots'")

            tier_record = await conn.fetchrow(
                tier_query,
                discount_id,
                tier["tier_order"],
                Decimal(tier["discount_value"]),
                tier["max_slots"],
                tier.get("display_fake_count", False),  # ⭐ حفظ إعداد العدد الوهمي
                tier.get("fake_count_value")  # ⭐ حفظ قيمة العدد الوهمي
            )
            # تحديث حالة المستوى الأول ليكون هو النشط
            if is_first_tier:
                await conn.execute("UPDATE discount_tiers SET is_active = true WHERE id = $1", tier_record['id'])
                is_first_tier = False

            created_tiers.append(dict(tier_record))

        result = dict(discount)
        result["tiers"] = created_tiers
        return result

    except Exception as e:
        logging.error(f"Error creating tiered discount: {e}", exc_info=True)
        raise

async def update_discount_tiers(conn, discount_id: int, new_tiers_data: List[Dict]):
    """
    يقوم بتحديث مستويات الخصم بذكاء:
    - يحدث المستويات الموجودة دون المساس بحالتها (used_slots, is_active).
    - يدرج المستويات الجديدة.
    - يحذف المستويات التي تمت إزالتها.
    """
    # 1. جلب الحالة الحالية للمستويات من قاعدة البيانات
    existing_tiers_raw = await conn.fetch("SELECT tier_order, used_slots, is_active FROM discount_tiers WHERE discount_id = $1", discount_id)
    existing_tiers_map = {t['tier_order']: t for t in existing_tiers_raw}
    existing_tier_orders = set(existing_tiers_map.keys())

    # 2. الحصول على قائمة المستويات الجديدة من البيانات المرسلة
    new_tier_orders = {t['tier_order'] for t in new_tiers_data}

    # 3. تحديد المستويات التي يجب حذفها
    tiers_to_delete = existing_tier_orders - new_tier_orders
    if tiers_to_delete:
        logging.info(f"Deleting tiers with orders {tiers_to_delete} for discount {discount_id}")
        await conn.execute("DELETE FROM discount_tiers WHERE discount_id = $1 AND tier_order = ANY($2::int[])", discount_id, list(tiers_to_delete))

    # 4. تحديث المستويات الحالية أو إدراج الجديدة (Upsert)
    # استخدام ON CONFLICT للحفاظ على حالة المستويات الحالية
    upsert_query = """
        INSERT INTO discount_tiers (
            discount_id, tier_order, discount_value, max_slots, used_slots, is_active,
            display_fake_count, fake_count_value
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (discount_id, tier_order) DO UPDATE
        SET
            discount_value = EXCLUDED.discount_value,
            max_slots = EXCLUDED.max_slots,
            display_fake_count = EXCLUDED.display_fake_count,
            fake_count_value = EXCLUDED.fake_count_value
        -- ملاحظة هامة: نحن لا نحدث used_slots أو is_active هنا للحفاظ على الحالة
    """

    for tier in new_tiers_data:
        order = tier['tier_order']
        # إذا كان المستوى موجوداً مسبقاً، نحافظ على used_slots و is_active
        # وإلا (مستوى جديد)، نبدأ من الصفر
        used_slots = existing_tiers_map.get(order, {}).get('used_slots', 0)
        is_active = existing_tiers_map.get(order, {}).get('is_active', False)

        # إذا لم يكن هناك أي مستوى نشط في قاعدة البيانات (حالة نادرة أو عند إنشاء خصم جديد عبر هذا المنطق)
        # قم بتفعيل المستوى الأول
        if not any(t['is_active'] for t in existing_tiers_map.values()) and order == min(new_tier_orders):
             is_active = True

        await conn.execute(
            upsert_query,
            discount_id,
            order,
            Decimal(tier['discount_value']),
            tier['max_slots'],
            used_slots, # <-- الحفاظ على القيمة القديمة
            is_active,  # <-- الحفاظ على القيمة القديمة
            tier.get('display_fake_count', False),
            tier.get('fake_count_value')
        )


async def get_current_active_tier(conn, discount_id: int) -> Optional[Dict]:
    """
    الحصول على المستوى النشط حالياً لخصم متدرج
    """
    query = """
        SELECT * FROM discount_tiers 
        WHERE discount_id = $1 
        AND is_active = true 
        AND used_slots < max_slots
        ORDER BY tier_order ASC
        LIMIT 1;
    """

    tier = await conn.fetchrow(query, discount_id)
    return dict(tier) if tier else None


async def get_next_tier_preview(conn, discount_id: int) -> Optional[Dict]:
    """
    معاينة المستوى التالي في التسعير المتدرج
    """
    current_tier = await get_current_active_tier(conn, discount_id)
    if not current_tier:
        return None

    next_tier_query = """
        SELECT * FROM discount_tiers 
        WHERE discount_id = $1 
        AND tier_order > $2
        ORDER BY tier_order ASC
        LIMIT 1;
    """

    next_tier = await conn.fetchrow(next_tier_query, discount_id, current_tier['tier_order'])
    return dict(next_tier) if next_tier else None


# ⭐ تعديل: تبسيط منطق الحجز والتركيز على المستوى الحالي فقط (نسخة مدمجة)
async def claim_tiered_discount_slot(conn, discount_id: int) -> Tuple[bool, Optional[Dict]]:
    """
    حجز مقعد في المستوى النشط الحالي. إذا امتلأ، يتم تعطيله وتفعيل المستوى التالي.
    يعيد (True, tier_data) عند النجاح, و (False, None) عند الفشل.
    """
    try:
        # قفل المستوى النشط الحالي لمنع حالات التسابق
        current_tier = await conn.fetchrow(
            """
            SELECT * FROM discount_tiers 
            WHERE discount_id = $1 AND is_active = true AND used_slots < max_slots
            ORDER BY tier_order ASC
            LIMIT 1
            FOR UPDATE;
            """,
            discount_id
        )

        if not current_tier:
            logging.warning(f"No active and available tier found for discount {discount_id}")
            return False, None

        # زيادة عداد الاستخدام
        new_used_slots = current_tier['used_slots'] + 1
        await conn.execute(
            "UPDATE discount_tiers SET used_slots = $1 WHERE id = $2",
            new_used_slots, current_tier['id']
        )

        # إذا امتلأ هذا المستوى، قم بتعطيله وتفعيل المستوى التالي إن وجد
        if new_used_slots >= current_tier['max_slots']:
            logging.info(f"Tier ID {current_tier['id']} is now full. Deactivating it.")
            await conn.execute("UPDATE discount_tiers SET is_active = false WHERE id = $1", current_tier['id'])

            # البحث عن المستوى التالي وتفعيله
            next_tier_to_activate = await conn.fetchrow(
                """
                UPDATE discount_tiers
                SET is_active = true
                WHERE id = (
                    SELECT id FROM discount_tiers
                    WHERE discount_id = $1 AND tier_order > $2
                    ORDER BY tier_order ASC
                    LIMIT 1
                )
                RETURNING id;
                """,
                discount_id, current_tier['tier_order']
            )

            if not next_tier_to_activate:
                logging.info(f"No next tier found for discount {discount_id}. Deactivating the parent discount.")
                await conn.execute("UPDATE discounts SET is_active = false WHERE id = $1", discount_id)

        logging.info(
            f"Successfully claimed slot in tier {current_tier['id']}. Usage: {new_used_slots}/{current_tier['max_slots']}")
        return True, dict(current_tier)

    except Exception as e:
        logging.error(f"Error claiming tiered discount slot: {e}", exc_info=True)
        raise


async def get_tiered_discount_display_info(conn, discount_id: int) -> Optional[Dict]:
    """
    الحصول على معلومات العرض للتسعير المتدرج (متوافق مع التعديلات)
    """
    # المستوى الحالي
    current_tier = await get_current_active_tier(conn, discount_id)
    if not current_tier:
        return None

    # المستوى التالي للمعاينة
    next_tier = await get_next_tier_preview(conn, discount_id)

    # معلومات الخصم الأساسية
    discount_info = await conn.fetchrow(
        """
        SELECT name, description, applicable_to_subscription_plan_id, 
               applicable_to_subscription_type_id, price_lock_duration_months
        FROM discounts WHERE id = $1
        """,
        discount_id
    )

    remaining_slots = current_tier['max_slots'] - current_tier['used_slots']

    # الحصول على العدد الوهمي إذا كان مفعلاً
    display_slots = remaining_slots
    if current_tier.get('display_fake_count') and current_tier.get('fake_count_value') is not None:
        display_slots = current_tier['fake_count_value']

    return {
        "discount_id": discount_id,
        "discount_name": discount_info['name'],
        "price_lock_duration_months": discount_info['price_lock_duration_months'],
        "current_tier": {
            "tier_id": current_tier['id'],
            "tier_order": current_tier['tier_order'],
            "discount_value": float(current_tier['discount_value']),
            "remaining_slots": display_slots,  # يعرض العدد الحقيقي أو الوهمي
            "total_slots": current_tier['max_slots']
        },
        "next_tier": {
            "tier_order": next_tier['tier_order'],
            "discount_value": float(next_tier['discount_value']),
            "total_slots": next_tier['max_slots']
        } if next_tier else None
    }


async def get_price_for_user_with_tiered(conn, telegram_id: int, plan_id: int) -> dict:
    """
    النسخة المحدثة التي تدعم التسعير المتدرج (متوافقة مع التعديلات)
    """
    # جلب معلومات الخطة والسعر الأساسي
    plan_info = await conn.fetchrow(
        "SELECT subscription_type_id, price FROM subscription_plans WHERE id = $1",
        plan_id
    )
    if not plan_info:
        # يمكنك تعديل هذا السلوك حسب الحاجة
        raise ValueError(f"Plan with ID {plan_id} not found.")

    base_price = Decimal(plan_info['price'])
    subscription_type_id = plan_info['subscription_type_id']

    # --- ⭐ التصحيح الرئيسي هنا ---
    # جلب السعر المثبت (إن وجد) مع تاريخ انتهاء صلاحيته باستخدام telegram_id
    locked_record = await conn.fetchrow(
        """
        SELECT ud.locked_price, d.name as discount_name, ud.tier_id, ud.expires_at
        FROM user_discounts ud
        JOIN users u ON u.id = ud.user_id
        LEFT JOIN discounts d ON d.id = ud.discount_id
        WHERE u.telegram_id = $1 AND ud.subscription_plan_id = $2 AND ud.is_active = true
        AND (ud.expires_at IS NULL OR ud.expires_at > NOW())
        """,
        telegram_id, plan_id
    )

    # جلب الخصومات العادية (لا تغيير هنا)
    regular_offers = await conn.fetch(
        """
        SELECT id as discount_id, name, discount_type, discount_value, lock_in_price
        FROM discounts
        WHERE (applicable_to_subscription_plan_id = $1 OR applicable_to_subscription_type_id = $2)
          AND is_active = true AND target_audience = 'all_new'
          AND (start_date IS NULL OR start_date <= NOW())
          AND (end_date IS NULL OR end_date >= NOW())
          AND (max_users IS NULL OR usage_count < max_users)
          AND is_tiered = false
        """,
        plan_id, subscription_type_id
    )

    # جلب الخصومات المتدرجة النشطة (لا تغيير هنا)
    tiered_offers = await conn.fetch(
        """
        SELECT d.id as discount_id, d.name, d.price_lock_duration_months, d.lock_in_price
        FROM discounts d
        WHERE (d.applicable_to_subscription_plan_id = $1 OR d.applicable_to_subscription_type_id = $2)
          AND d.is_active = true AND d.target_audience = 'all_new'
          AND (d.start_date IS NULL OR d.start_date <= NOW())
          AND (d.end_date IS NULL OR d.end_date >= NOW())
          AND d.is_tiered = true
        """,
        plan_id, subscription_type_id
    )

    price_options = []

    # السعر الأساسي
    price_options.append({
        'price': base_price,
        'discount_id': None,
        'discount_name': 'Base Price',
        'lock_in_price': False,
        'tier_info': None
    })

    # السعر المثبت
    if locked_record and locked_record['locked_price'] is not None:
        tier_info = {'tier_id': locked_record['tier_id']} if locked_record['tier_id'] else None
        price_options.append({
            'price': Decimal(locked_record['locked_price']),
            'discount_id': None,
            'discount_name': locked_record.get('discount_name') or 'Locked-in Deal',
            'lock_in_price': True,
            'tier_info': tier_info
        })

    # دالة مساعدة لحساب السعر بعد الخصم
    def calculate_discounted_price(price, discount_type, value):
        if discount_type == 'percentage':
            return price * (1 - (Decimal(value) / 100))
        elif discount_type == 'fixed_amount':
            return price - Decimal(value)
        return price

    # الخصومات العادية
    for offer in regular_offers:
        price_options.append({
            'price': calculate_discounted_price(base_price, offer['discount_type'], offer['discount_value']),
            'discount_id': offer['discount_id'],
            'discount_name': offer['name'],
            'lock_in_price': offer['lock_in_price'],
            'tier_info': None
        })

    # الخصومات المتدرجة
    for tiered_offer in tiered_offers:
        current_tier = await get_current_active_tier(conn, tiered_offer['discount_id'])
        if current_tier:
            tier_info = {
                'tier_id': current_tier['id'],
                'tier_order': current_tier['tier_order'],
                'remaining_slots': current_tier['max_slots'] - current_tier['used_slots'],
                'total_slots': current_tier['max_slots'],
                'price_lock_duration_months': tiered_offer['price_lock_duration_months']
            }
            price_options.append({
                'price': calculate_discounted_price(base_price, 'percentage', current_tier['discount_value']),
                'discount_id': tiered_offer['discount_id'],
                'discount_name': tiered_offer['name'],
                'lock_in_price': tiered_offer['lock_in_price'],
                'tier_info': tier_info
            })

    # اختيار أفضل سعر (الأقل)
    best_option = min(price_options, key=lambda x: x['price'])
    return best_option

async def claim_discount_slot_universal(conn, discount_id: int) -> Tuple[bool, Optional[Dict]]:
    """
    دالة موحدة لحجز مقعد في أي نوع من الخصومات (متوافقة مع التعديلات)
    """
    if not discount_id:
        return True, None

    # تحقق من نوع الخصم
    discount_info = await conn.fetchrow(
        "SELECT is_tiered FROM discounts WHERE id = $1",  # تم التحديث هنا
        discount_id
    )

    if not discount_info:
        return False, None

    if discount_info['is_tiered']:
        # التسعير المتدرج
        return await claim_tiered_discount_slot(conn, discount_id)
    else:
        # الخصم العادي المحدود
        success = await claim_limited_discount_slot(conn, discount_id)
        return success, None


async def claim_limited_discount_slot(conn, discount_id: int) -> bool:
    """
    تقوم بحجز مقعد واحد لخصم محدود العدد بشكل آمن (atomic).
    تعيد True عند النجاح، و False عند الفشل (إذا كانت المقاعد قد امتلأت).
    """
    if not discount_id:
        return True

    try:
        discount = await conn.fetchrow(
            "SELECT max_users, usage_count FROM discounts WHERE id = $1 FOR UPDATE",
            discount_id
        )

        if not discount or discount['max_users'] is None:
            return True

        if discount['usage_count'] >= discount['max_users']:
            logging.warning(f"Attempted to claim a slot for fully used discount ID: {discount_id}. Aborting.")
            await conn.execute("UPDATE discounts SET is_active = false WHERE id = $1 AND is_active = true", discount_id)
            return False

        new_usage_count = discount['usage_count'] + 1
        await conn.execute(
            "UPDATE discounts SET usage_count = $1 WHERE id = $2",
            new_usage_count, discount_id
        )

        logging.info(f"Successfully claimed slot for discount {discount_id}. New count: {new_usage_count}")

        if new_usage_count >= discount['max_users']:
            logging.info(f"Discount {discount_id} has reached its limit. Deactivating.")
            await conn.execute("UPDATE discounts SET is_active = false WHERE id = $1", discount_id)

        return True

    except Exception as e:
        logging.error(f"Error claiming discount slot for ID {discount_id}: {e}", exc_info=True)
        raise e


# ⭐ تعديل: إضافة expires_at للتعامل مع مدة تثبيت السعر (نسخة مدمجة)
async def save_user_discount(conn, user_id: int, plan_id: int, discount_id: int,
                             locked_price: Decimal, tier_info: Optional[Dict],
                             lock_duration_months: Optional[int], is_active: bool): # <-- إضافة جديدة
    """
    حفظ أو تحديث سجل خصم المستخدم.
    إذا كان is_active = true، فإنه يمثل سعراً مثبتاً.
    إذا كان is_active = false، فإنه يمثل سجلاً تاريخياً لاستخدام الخصم مرة واحدة.
    """
    expires_at = None
    # نحسب تاريخ الانتهاء فقط إذا كان السجل نشطاً وله مدة
    if is_active and lock_duration_months and lock_duration_months > 0:
        expires_at = datetime.now(timezone.utc) + relativedelta(months=lock_duration_months)

    tier_id = tier_info.get('tier_id') if tier_info else None

    # إذا كان السجل نشطاً، نستخدم ON CONFLICT لتحديث السعر المثبت الحالي.
    if is_active:
        query = """
            INSERT INTO user_discounts (
                user_id, subscription_plan_id, discount_id, locked_price, tier_id,
                is_active, granted_at, expires_at
            ) VALUES ($1, $2, $3, $4, $5, true, NOW(), $6)
            ON CONFLICT (user_id, subscription_plan_id, is_active)
            WHERE is_active = true
            DO UPDATE SET
                discount_id = EXCLUDED.discount_id,
                locked_price = EXCLUDED.locked_price,
                tier_id = EXCLUDED.tier_id,
                granted_at = EXCLUDED.granted_at,
                expires_at = EXCLUDED.expires_at;
        """
        await conn.execute(query, user_id, plan_id, discount_id, locked_price, tier_id, expires_at)
    else:
        # إذا كان السجل غير نشط، فهو مجرد توثيق. نقوم بإدخال بسيط.
        # لا حاجة لـ ON CONFLICT لأن is_active=false لن يتعارض مع القيد الفريد.
        query = """
            INSERT INTO user_discounts (
                user_id, subscription_plan_id, discount_id, locked_price, tier_id,
                is_active, granted_at, expires_at
            ) VALUES ($1, $2, $3, $4, $5, false, NOW(), NULL);
        """
        await conn.execute(query, user_id, plan_id, discount_id, locked_price, tier_id)