from quart import Blueprint, request, jsonify, current_app
import logging
import re
from aiogram import Bot
from config import TELEGRAM_BOT_TOKEN
from database.db_queries import get_user, add_user, get_user_subscriptions
from datetime import datetime

user_bp = Blueprint("users", __name__)

# تهيئة البوت لجلب بيانات المستخدم من Telegram API
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)

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
        user = await telegram_bot.get_chat(telegram_id)
        full_name = clean_name(user.full_name) if user.full_name else "N/L"
        username = user.username if user.username else "N/L"
        return full_name, username
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب بيانات المستخدم {telegram_id}: {e}")
        return "N/L", "N/L"


async def get_telegram_profile_photo(telegram_id: int) -> str:
    """ 🔹 جلب صورة الملف الشخصي للمستخدم من Telegram API أو إرجاع الصورة الافتراضية """
    try:
        user_photos = await telegram_bot.get_user_profile_photos(user_id=telegram_id, limit=1)
        if user_photos.photos:
            file_id = user_photos.photos[0][0].file_id
            return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب صورة المستخدم {telegram_id}: {e}")

    return DEFAULT_PROFILE_PHOTO


@user_bp.route("/api/user", methods=["GET"])
async def get_user_info():
    """
    🔹 جلب بيانات المستخدم والاشتراكات بناءً على `telegram_id`
    ✅ التحقق من قاعدة البيانات
    ✅ تحديث البيانات من `Telegram API` عند كل طلب
    ✅ إرجاع البيانات المحدثة مع الاشتراكات
    """
    telegram_id = request.args.get("telegram_id")

    if not telegram_id or not telegram_id.isdigit():
        return jsonify({"error": "Missing or invalid telegram_id"}), 400

    telegram_id = int(telegram_id)

    try:
        async with current_app.db_pool.acquire() as conn:
            user = await get_user(conn, telegram_id)
            full_name, username = await get_telegram_user_info(telegram_id)
            profile_photo = await get_telegram_profile_photo(telegram_id)
            subscriptions = await get_user_subscriptions(conn, telegram_id)

            # تحديث بيانات المستخدم إذا لزم الأمر
            if not user or (user['full_name'] != full_name) or (user['username'] != username):
                await add_user(conn, telegram_id, username=username, full_name=full_name)

            subscription_list = []
            for sub in subscriptions:
                expiry_date = sub['expiry_date']
                now = datetime.now()

                # حساب المدة المتبقية بدقة
                if expiry_date < now:
                    days_left = 0
                    status = "منتهي"
                    progress = 0
                else:
                    delta = expiry_date - now
                    days_left = delta.days + 1  # +1 لاحتساب اليوم الحالي
                    total_days = (expiry_date - sub['start_date']).days  # نفترض وجود حقل start_date
                    progress = min(int((days_left / total_days) * 100), 100) if total_days > 0 else 0
                    status = "نشط" if days_left > 0 else "منتهي"

                # تحسين تنسيق الرسائل
                expiry_msg = "انتهى الاشتراك" if days_left == 0 else f"متبقي {days_left} يوم"

                subscription_list.append({
                    "id": sub['subscription_type_id'],
                    "name": sub['subscription_name'],
                    "price": f"{sub['price']:.2f} دولار/شهر",  # تنسيق السعر
                    "expiry": expiry_msg,
                    "progress": progress,
                    "status": status,
                    "expiry_date": expiry_date.isoformat()  # إضافة تاريخ الانتهاء الدقيق
                })

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