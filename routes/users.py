from quart import Blueprint, request, jsonify, current_app
import logging
import re
import pytz
from database.db_queries import get_user, add_user, get_user_subscriptions
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا

user_bp = Blueprint("users", __name__)

# المسار الافتراضي لصورة الملف الشخصي
DEFAULT_PROFILE_PHOTO = "/static/default_profile.png"


def clean_name(full_name: str) -> str:
    """ 🔹 تنظيف الاسم من الرموز التعبيرية والعلامات الخاصة """
    if not full_name:
        return "N/L"
    emoji_pattern = re.compile("[\U00010000-\U0010ffff]", flags=re.UNICODE)
    return emoji_pattern.sub('', full_name).strip()


async def get_telegram_user_info(telegram_id: int):
    """ 🔹 جلب بيانات المستخدم (الاسم واسم المستخدم) من Telegram API """
    try:
        telegram_bot = current_app.bot  # ✅ استخدام البوت الموجود في `app.py`
        user = await telegram_bot.get_chat(telegram_id)
        full_name = clean_name(user.full_name) if user.full_name else "N/L"
        username = f"@{user.username}" if user.username else "N/L"
        return full_name, username
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب بيانات المستخدم {telegram_id}: {e}")
        return "N/L", "N/L"


async def get_telegram_profile_photo(telegram_id: int) -> str:
    """ 🔹 جلب صورة الملف الشخصي للمستخدم من Telegram API بشكل صحيح """
    try:
        telegram_bot = current_app.bot  # ✅ استخدام البوت الموجود في `app.py`
        user_photos = await telegram_bot.get_user_profile_photos(user_id=telegram_id, limit=1)
        if user_photos.photos:
            file = await telegram_bot.get_file(user_photos.photos[0][0].file_id)
            return f"https://api.telegram.org/file/bot{current_app.config['TELEGRAM_BOT_TOKEN']}/{file.file_path}"
        return DEFAULT_PROFILE_PHOTO
    except Exception as e:
        logging.error(f"❌ خطأ في جلب صورة المستخدم {telegram_id}: {str(e)}")
        return DEFAULT_PROFILE_PHOTO


@user_bp.route("/api/user", methods=["GET"])
async def get_user_info():
    """
    🔹 جلب بيانات المستخدم والاشتراكات بناءً على `telegram_id`
    ✅ التحقق من قاعدة البيانات
    ✅ تحديث البيانات من `Telegram API` عند كل طلب
    ✅ إرجاع البيانات المحدثة مع الاشتراكات
    ✅ تحسين التعامل مع حالة `is_active`
    """
    telegram_id = request.args.get("telegram_id")

    if not telegram_id or not telegram_id.isdigit():
        return jsonify({"error": "Missing or invalid telegram_id"}), 400

    telegram_id = int(telegram_id)

    try:
        async with current_app.db_pool.acquire() as conn:
            # 🔹 جلب بيانات المستخدم من قاعدة البيانات
            user = await get_user(conn, telegram_id)

            # 🔹 جلب البيانات الحقيقية من Telegram API
            full_name, username = await get_telegram_user_info(telegram_id)
            profile_photo = await get_telegram_profile_photo(telegram_id)

            # ✅ تحديث بيانات المستخدم إذا تغيرت
            if not user or (user['full_name'] != full_name) or (user['username'] != username):
                await add_user(conn, telegram_id, username=username, full_name=full_name)

            # 🔹 جلب بيانات الاشتراكات من قاعدة البيانات
            subscriptions = await get_user_subscriptions(conn, telegram_id)

            # ✅ ضبط التوقيت المحلي (UTC+3 الرياض)
            local_tz = pytz.timezone("Asia/Riyadh")
            now = datetime.now(timezone.utc).astimezone(local_tz)

            subscription_list = []
            for sub in subscriptions:
                expiry_date = sub['expiry_date']
                start_date = sub['start_date'] if sub['start_date'] else expiry_date - timedelta(days=30)

                # ✅ التأكد من ضبط timezone
                if expiry_date.tzinfo is None:
                    expiry_date = expiry_date.replace(tzinfo=timezone.utc)
                if start_date.tzinfo is None:
                    start_date = start_date.replace(tzinfo=timezone.utc)

                expiry_date = expiry_date.astimezone(local_tz)
                start_date = start_date.astimezone(local_tz)

                # ✅ حساب مدة الاشتراك والتقدم
                total_days = (expiry_date - start_date).days if start_date else 30
                days_left = max((expiry_date - now).days, 0)
                progress = min(int((days_left / total_days) * 100), 100) if total_days > 0 else 0

                # ✅ التحقق من حالة `is_active` الحقيقية في قاعدة البيانات
                is_active = sub['is_active']  # 🔹 استرجاع الحالة الفعلية من قاعدة البيانات
                if is_active:
                    status = "نشط"  # ✅ إذا كان `is_active = True` فهو نشط
                else:
                    status = "منتهي"  # ❌ إذا كان `is_active = False` فهو منتهي

                expiry_msg = "انتهى الاشتراك" if not is_active else f"متبقي {days_left} يوم"

                subscription_list.append({
                    "id": sub['subscription_type_id'],
                    "name": sub['subscription_name'],
                    "price": f"{sub['price']:.2f} دولار/شهر",
                    "expiry": expiry_msg,
                    "progress": progress,
                    "status": status,
                    "expiry_date": expiry_date.isoformat()
                })

            # ✅ إرجاع البيانات المحدثة
            return jsonify({
                "telegram_id": telegram_id,
                "full_name": full_name,
                "username": username,
                "profile_photo": profile_photo,
                "subscriptions": subscription_list
            }), 200

    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب بيانات المستخدم {telegram_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500
