from quart import Blueprint, request, jsonify, current_app
import logging
import pytz
from datetime import datetime, timedelta, timezone
from database.db_queries import get_user_subscriptions
from typing import Dict, Any

user_bp = Blueprint("users", __name__)
DEFAULT_PROFILE_PHOTO = "/static/default_profile.png"


def handle_date_timezone(dt: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """معالجة التواريخ وإضافة المنطقة الزمنية إذا لم تكن موجودة"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def calculate_subscription_details(sub: Dict[str, Any], local_tz: pytz.BaseTzInfo) -> Dict[str, Any]:
    """حساب تفاصيل الاشتراك مع تحسين عرض الوقت المتبقي"""
    expiry_date = handle_date_timezone(sub['expiry_date'], local_tz)
    start_date = sub['start_date'] or expiry_date - timedelta(days=30)
    start_date = handle_date_timezone(start_date, local_tz)

    now = datetime.now(local_tz)
    # حساب الفرق بالثواني
    seconds_left = max((expiry_date - now).total_seconds(), 0)
    days_left = int(seconds_left // (60 * 60 * 24))  # تقريب لعدد صحيح

    # حساب إجمالي مدة الاشتراك
    total_seconds = (expiry_date - start_date).total_seconds()
    total_days = max(int(total_seconds // (60 * 60 * 24)), 1)  # ضمان أن لا يكون صفرًا

    progress = 0
    if total_days > 0:
        progress = min(int((days_left / total_days) * 100), 100)

    is_active = sub['is_active'] and days_left > 0
    status = "نشط" if is_active else "منتهي"

    # تحديد نص انتهاء الاشتراك
    if days_left == 0:
        expiry_text = "متبقي أقل من يوم"
    else:
        expiry_text = f"متبقي {days_left} يوم"

    return {
        "id": sub['subscription_type_id'],
        "name": sub['subscription_name'],
        "expiry": expiry_text if is_active else "انتهى الاشتراك",
        "progress": progress,
        "status": status,
        "start_date": start_date.isoformat(),
        "expiry_date": expiry_date.isoformat(),
        "invite_link": sub.get('invite_link')  # <-- إضافة هذا الحقل
    }

@user_bp.route("/api/user/subscriptions", methods=["GET"])
async def get_user_subscriptions_endpoint():
    """جلب اشتراكات المستخدم فقط"""
    telegram_id = request.args.get("telegram_id")

    if not telegram_id or not telegram_id.isdigit():
        return jsonify({
            "error": "رقم تليجرام غير صالح",
            "ar_message": "الرجاء إدخال رقم مستخدم تليجرام صحيح"
        }), 400

    try:
        telegram_id_int = int(telegram_id)
        local_tz = pytz.timezone("Asia/Riyadh")

        async with current_app.db_pool.acquire() as conn:
            subscriptions = await get_user_subscriptions(conn, telegram_id_int)
            subscription_list = [calculate_subscription_details(sub, local_tz) for sub in subscriptions]

            return jsonify({
                "telegram_id": telegram_id,
                "subscriptions": subscription_list
            }), 200

    except Exception as e:
        logging.error(f"خطأ في جلب الاشتراكات: {str(e)}", exc_info=True)
        return jsonify({
            "error": "Internal Server Error",
            "ar_message": "حدث خطأ تقني، الرجاء المحاولة لاحقاً"
        }), 500