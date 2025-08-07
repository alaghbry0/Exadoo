from quart import Blueprint, request, jsonify, current_app
import logging
import pytz
from datetime import datetime, timedelta, timezone
from database.db_queries import get_user_subscriptions, upsert_user, link_user_gmail
from typing import Dict, Any

user_bp = Blueprint("users", __name__)
DEFAULT_PROFILE_PHOTO = "/static/default_profile.png"


# 💡=============== إضافة جديدة: نقطة API لمزامنة بيانات المستخدم ===============💡
@user_bp.route("/api/user/sync", methods=["POST"])
async def sync_user_profile():
    """
    نقطة API لإضافة مستخدم جديد أو تحديث بياناته الحالية (UPSERT).
    تُستدعى هذه النقطة في كل مرة يفتح فيها المستخدم التطبيق المصغر.
    """
    try:
        data = await request.get_json()
        telegram_id = data.get("telegramId")
        username = data.get("telegramUsername")
        full_name = data.get("fullName")

        # التحقق من وجود البيانات الأساسية
        if not telegram_id:
            return jsonify({"error": "telegramId is required"}), 400

        async with current_app.db_pool.acquire() as connection:
            # 3. استدعاء الدالة المنفصلة وتمرير البيانات إليها
            success = await upsert_user(connection, int(telegram_id), username, full_name)

        # 4. التحقق من نتيجة العملية
        if success:
            logging.info(f"User sync API call successful for telegram_id={telegram_id}")
            return jsonify({"status": "success", "message": "User data synced"}), 200
        else:
            return jsonify({
                "error": "Internal Server Error",
                "ar_message": "حدث خطأ أثناء مزامنة بيانات المستخدم"
            }), 500

    except Exception as e:
        # هذا الـ try/except لا يزال مفيدًا لالتقاط أخطاء أخرى مثل
        # فشل قراءة JSON أو خطأ في تحويل telegram_id إلى int
        logging.error(f"Error in sync_user_profile endpoint: {str(e)}", exc_info=True)
        return jsonify({
            "error": "Internal Server Error",
            "ar_message": "حدث خطأ أثناء معالجة الطلب"
        }), 500


def handle_date_timezone(dt: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """معالجة التواريخ وإضافة المنطقة الزمنية إذا لم تكن موجودة"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def calculate_subscription_details(sub: Dict[str, Any], local_tz: pytz.BaseTzInfo) -> Dict[str, Any]:
    """
    [مُعدل] حساب تفاصيل الاشتراك مع إضافة روابط الدعوة للقنوات الرئيسية والفرعية.
    """
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
        # <-- ✨ التعديل الأساسي هنا ✨ -->
        # تم تغيير اسم الحقل إلى 'main_invite_link' ليعكس أنه رابط القناة الرئيسية
        "invite_link": sub.get('main_invite_link'),
        # إضافة حقل جديد يحتوي على قائمة بالقنوات الفرعية، مع التأكد من إرجاع قائمة فارغة بدلاً من null
        "sub_channel_links": sub.get('sub_channel_links') or []
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

# 💡=============== نقطة API لاستقبال بيانات الربط من تطبيق الموبايل ===============💡
@user_bp.route("/api/v1/users/link-account", methods=["POST"])
async def link_mobile_account():
    """
    تستقبل هذه النقطة طلب POST من خادم تطبيق الموبايل لربط حساب المستخدم
    عن طريق إضافة بريده الإلكتروني.
    """
    # الخطوة 1: التحقق من مفتاح الـ API السري (الأمان أولاً)
    auth_header = request.headers.get("Authorization")
    expected_key = f"Bearer {os.getenv('MOBILE_API_KEY')}"

    if not auth_header or auth_header != expected_key:
        logging.warning("Unauthorized attempt to access link-account API.")
        return jsonify({"error": "Forbidden"}), 403

    try:
        # الخطوة 2: قراءة البيانات من الطلب
        data = await request.get_json()
        telegram_id = data.get("telegram_id")
        user_gmail = data.get("user_gmail")

        # الخطوة 3: التحقق من وجود البيانات الأساسية
        if not telegram_id or not user_gmail:
            return jsonify({"error": "telegram_id and user_gmail are required"}), 400

        # الخطوة 4: تنفيذ الربط في قاعدة البيانات
        async with current_app.db_pool.acquire() as connection:
            success = await link_user_gmail(connection, int(telegram_id), user_gmail)

        # الخطوة 5: إرجاع الرد المناسب
        if success:
            logging.info(f"Account linking successful for telegram_id={telegram_id}")
            return jsonify({"status": "success", "message": "Account linked successfully"}), 200
        else:
            # قد يكون السبب أن المستخدم غير موجود أو خطأ في قاعدة البيانات
            return jsonify({
                "error": "Failed to link account",
                "ar_message": "فشل ربط الحساب، قد يكون المستخدم غير موجود"
            }), 404

    except Exception as e:
        logging.error(f"Error in link_mobile_account endpoint: {str(e)}", exc_info=True)
        return jsonify({
            "error": "Internal Server Error",
            "ar_message": "حدث خطأ داخلي في الخادم"
        }), 500
