from quart import Blueprint, request, jsonify, current_app
import logging
import re
import os
import pytz
from database.db_queries import get_user, add_user, get_user_subscriptions
from datetime import datetime, timedelta, timezone  # <-- تأكد من وجود timezone هنا


user_bp = Blueprint("users", __name__)

# المسار الافتراضي لصورة الملف الشخصي
DEFAULT_PROFILE_PHOTO = "/static/default_profile.png"


@user_bp.route("/api/user", methods=["GET"])
async def get_user_info():
    """
    🔹 جلب بيانات المستخدم والاشتراكات بناءً على `telegram_id`
    ✅ التحقق من قاعدة البيانات
    ✅ **لم يعد يتم تحديث البيانات من Telegram API عند كل طلب**
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

            # ✅ **لن يتم جلب البيانات الحقيقية من Telegram API بعد الآن**
            full_name = user['full_name'] if user and user['full_name'] else "N/L" # ✅ استخراج الاسم من قاعدة البيانات
            username = user['username'] if user and user['username'] else "N/L" # ✅ استخراج اسم المستخدم من قاعدة البيانات
            profile_photo = DEFAULT_PROFILE_PHOTO # ✅ استخدام الصورة الافتراضية مؤقتًا، يمكن جلبها من قاعدة البيانات لاحقًا إذا لزم الأمر

            # ✅ تحديث بيانات المستخدم إذا لم يكن موجودًا فقط (لأول مرة)
            if not user:
                # ✅ في المستقبل، يمكن استدعاء Telegram API هنا لجلب البيانات لأول مرة وتخزينها في قاعدة البيانات
                # full_name, username = await get_telegram_user_info(telegram_id)
                # profile_photo = await get_telegram_profile_photo(telegram_id)
                await add_user(conn, telegram_id, username=username, full_name=full_name) # ✅ إضافة مستخدم جديد حتى لو كانت البيانات الأساسية غير متوفرة في الوقت الحالي

            # 🔹 جلب بيانات الاشتراكات من قاعدة البيانات (كما هو الحال سابقًا)
            subscriptions = await get_user_subscriptions(conn, telegram_id)

            # ✅ ضبط التوقيت المحلي (UTC+3 الرياض) (كما هو الحال سابقًا)
            local_tz = pytz.timezone("Asia/Riyadh")
            now = datetime.now(timezone.utc).astimezone(local_tz)

            subscription_list = []
            for sub in subscriptions:
                expiry_date = sub['expiry_date']
                start_date = sub['start_date'] if sub['start_date'] else expiry_date - timedelta(days=30)

                # ✅ التأكد من ضبط timezone (كما هو الحال سابقًا)
                if expiry_date.tzinfo is None:
                    expiry_date = expiry_date.replace(tzinfo=timezone.utc)
                if start_date.tzinfo is None:
                    start_date = start_date.replace(tzinfo=timezone.utc)

                expiry_date = expiry_date.astimezone(local_tz)
                start_date = start_date.astimezone(local_tz)

                # ✅ حساب مدة الاشتراك والتقدم (كما هو الحال سابقًا)
                total_days = (expiry_date - start_date).days if start_date else 30
                days_left = max((expiry_date - now).days, 0)
                progress = min(int((days_left / total_days) * 100), 100) if total_days > 0 else 0

                # ✅ التحقق من حالة `is_active` الحقيقية في قاعدة البيانات (كما هو الحال سابقًا)
                is_active = sub['is_active']
                status = "نشط" if is_active else "منتهي"

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

            # ✅ إرجاع البيانات المحدثة (كما هو الحال سابقًا)
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