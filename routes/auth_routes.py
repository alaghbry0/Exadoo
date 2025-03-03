import jwt
import datetime
import os  # لإدارة متغيرات البيئة
from quart import Blueprint, request, jsonify, abort, current_app, make_response
from auth import verify_google_token

SECRET_KEY = os.getenv("SECRET_KEY", "your_default_secret_key")
REFRESH_SECRET_KEY = os.getenv("REFRESH_SECRET_KEY", "your_refresh_secret_key")  # مفتاح خاص بالتجديد
REFRESH_COOKIE_NAME = os.getenv("REFRESH_COOKIE_NAME", "refresh_token") # اسم ملف تعريف الارتباط للتجديد

def generate_tokens(email, role):
    """إنشاء Access Token و Refresh Token"""
    access_token = jwt.encode({
        "email": email,
        "role": role,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=30)  # توكن مؤقت (30 دقيقة)
    }, SECRET_KEY, algorithm="HS256")

    refresh_token = jwt.encode({
        "email": email,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)  # توكن صالح لسبعة أيام
    }, REFRESH_SECRET_KEY, algorithm="HS256")

    return access_token, refresh_token

auth_routes = Blueprint("auth_routes", __name__, url_prefix="/api/auth")

@auth_routes.route("/login", methods=["POST"])
async def login():
    """تسجيل الدخول باستخدام Google OAuth"""
    data = await request.get_json()
    token = data.get("id_token")

    if not token:
        abort(400, description="Missing ID Token")

    user_info = await verify_google_token(token)
    if not user_info:
        abort(401, description="Invalid Token")

    email = user_info["email"]

    async with current_app.db_pool.acquire() as connection:
        user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)

        if not user:
            abort(403, description="Access denied")

        access_token, refresh_token = generate_tokens(email, user["role"])

        response = jsonify({ # لا نرسل refresh_token في JSON بعد الآن
            "access_token": access_token,
            "role": user["role"]
        })

        # تعيين refresh_token كـ HTTP-only Cookie
        response.set_cookie(
            REFRESH_COOKIE_NAME,
            refresh_token,
            httponly=True,
            secure=True,  # تأكد من استخدام HTTPS في الإنتاج
            samesite='Strict',
            max_age=7 * 24 * 3600  # 7 أيام (نفس مدة صلاحية التوكن)
        )
        return response

@auth_routes.route("/refresh", methods=["POST"])
async def refresh():
    """تجديد Access Token باستخدام Refresh Token من HTTP-only Cookie"""
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME) # استخراج refresh_token من ملف تعريف الارتباط

    if not refresh_token:
        abort(400, description="Missing Refresh Token Cookie") # تم التعديل في رسالة الخطأ

    try:
        payload = jwt.decode(refresh_token, REFRESH_SECRET_KEY, algorithms=["HS256"])
        email = payload["email"]
    except jwt.ExpiredSignatureError:
        abort(401, description="Refresh token expired")
    except jwt.InvalidTokenError:
        abort(401, description="Invalid refresh token")

    async with current_app.db_pool.acquire() as connection:
        user = await connection.fetchrow("SELECT * FROM panel_users WHERE email = $1", email)
        if not user:
            abort(403, description="User not found")

        access_token, new_refresh_token = generate_tokens(email, user["role"]) # توليد refresh_token جديد

        response = jsonify({"access_token": access_token})

        # تعيين refresh_token جديد كـ HTTP-only Cookie (تدوير التوكن)
        response.set_cookie(
            REFRESH_COOKIE_NAME,
            new_refresh_token,
            httponly=True,
            secure=True,  # تأكد من استخدام HTTPS في الإنتاج
            samesite='Strict',
            max_age=7 * 24 * 3600  # 7 أيام (نفس مدة صلاحية التوكن)
        )
        return response