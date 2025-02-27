import jwt
import datetime
import os  # لإدارة متغيرات البيئة
from quart import Blueprint, request, jsonify, abort, current_app
from auth import verify_google_token, get_current_user
from config import REFRESH_SECRET_KEY, SECRET_KEY


SECRET_KEY = os.getenv("SECRET_KEY", "your_default_secret_key")
REFRESH_SECRET_KEY = os.getenv("REFRESH_SECRET_KEY", "your_refresh_secret_key")  # مفتاح خاص بالتجديد

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

    return jsonify({
        "access_token": access_token,
        "refresh_token": refresh_token,  # إرسال التوكن المخصص للتجديد للواجهة الأمامية
        "role": user["role"]
    })

@auth_routes.route("/refresh", methods=["POST"])
async def refresh():
    """تجديد Access Token باستخدام Refresh Token"""
    data = await request.get_json()
    refresh_token = data.get("refresh_token")

    if not refresh_token:
        abort(400, description="Missing Refresh Token")

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

    new_access_token = jwt.encode({
        "email": email,
        "role": user["role"],
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
    }, SECRET_KEY, algorithm="HS256")

    return jsonify({"access_token": new_access_token})
