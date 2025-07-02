import os
import jwt
import datetime
from quart import Blueprint, request, jsonify, current_app

# تأكد من أن مسار الاستيراد هذا صحيح لمشروعك
from auth import verify_google_token

# ======================================================================
# ========= 🔵 إعدادات الـ Blueprint و CORS =========
# ======================================================================

auth_routes = Blueprint("auth_routes", __name__, url_prefix="/api/auth")

# ======================================================================
# ========= ⚙️ الثوابت والدوال المساعدة =========
# ======================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "your_default_secret_key")
REFRESH_SECRET_KEY = os.getenv("REFRESH_SECRET_KEY", "your_refresh_secret_key")
REFRESH_COOKIE_NAME = "refresh_token"


def generate_tokens(email, role):
    """إنشاء Access Token و Refresh Token."""
    now = datetime.datetime.now(datetime.timezone.utc)
    access_payload = {
        "email": email,
        "role": role,
        "exp": now + datetime.timedelta(minutes=30)  # صلاحية قصيرة للـ Access Token
    }
    refresh_payload = {
        "email": email,
        "exp": now + datetime.timedelta(days=7)  # صلاحية طويلة للـ Refresh Token
    }
    access_token = jwt.encode(access_payload, SECRET_KEY, algorithm="HS256")
    refresh_token = jwt.encode(refresh_payload, REFRESH_SECRET_KEY, algorithm="HS256")
    return access_token, refresh_token


def get_cookie_settings():
    """
    ✅✅ الحل النهائي الحقيقي: إعدادات كوكي ذكية تتكيف مع البيئة.
    """
    # تحقق مما إذا كنا في بيئة الإنتاج.
    # افترض أننا في الإنتاج إذا لم تكن تعمل على localhost.
    # هذا أفضل من الاعتماد على متغيرات البيئة التي قد تنسى ضبطها.
    is_production = "localhost" not in request.host.lower()

    if is_production:
        # --- إعدادات الإنتاج (e.g., https://exaado-panel.vercel.app) ---
        return {
            "path": "/",
            "httponly": True,
            "secure": True,      # ✅ يجب أن يكون True في الإنتاج
            "samesite": "None",  # ✅ ضروري لـ Cross-Site على HTTPS
            "max_age": 7 * 24 * 3600
        }
    else:
        # --- إعدادات التطوير (e.g., http://localhost:5000) ---
        return {
            "path": "/",
            "httponly": True,
            "secure": False,     # ✅ يجب أن يكون False على HTTP
            "samesite": "Lax",   # ✅✅ الحل: 'Lax' هو الخيار الأفضل لـ localhost
                                 # لأنه يعمل عبر المنافذ المختلفة دون الحاجة لـ 'Secure'.
            "max_age": 7 * 24 * 3600
        }


# ======================================================================
# ========= 🔑 مسارات المصادقة (Login, Refresh, Logout) =========
# ======================================================================

@auth_routes.route("/login", methods=["POST"])
async def login():
    """تسجيل الدخول وإعداد الـ Refresh Token كـ HttpOnly Cookie."""
    data = await request.get_json()
    if not data or "id_token" not in data:
        return jsonify({"error": "Missing ID token"}), 400

    user_info = await verify_google_token(data["id_token"])
    if not user_info:
        return jsonify({"error": "Invalid ID token"}), 401

    email = user_info["email"]
    async with current_app.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT role FROM panel_users WHERE email = $1", email)
        if not user:
            return jsonify({"error": "Access denied. User not found."}), 403

    access_token, refresh_token = generate_tokens(email, user["role"])

    response = jsonify({"access_token": access_token, "role": user["role"]})
    response.set_cookie(REFRESH_COOKIE_NAME, refresh_token, **get_cookie_settings())
    return response


@auth_routes.route("/refresh", methods=["POST"])
async def refresh():
    """تجديد الـ Access Token باستخدام الـ Refresh Token من الكوكي."""
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        current_app.logger.warning("Refresh attempt failed: No refresh token cookie found.")
        return jsonify({"error": "Authentication required. Please log in again."}), 401

    try:
        payload = jwt.decode(refresh_token, REFRESH_SECRET_KEY, algorithms=["HS256"])
        email = payload["email"]
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Session expired. Please log in again."}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid session. Please log in again."}), 401

    async with current_app.db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT role FROM panel_users WHERE email = $1", email)
        if not user:
            return jsonify({"error": "User associated with token not found."}), 403

    # لا حاجة لتجديد الـ Refresh Token في كل مرة، لكن تجديده يزيد الأمان
    access_token, new_refresh_token = generate_tokens(email, user["role"])

    response = jsonify({"access_token": access_token})
    response.set_cookie(REFRESH_COOKIE_NAME, new_refresh_token, **get_cookie_settings())
    return response


@auth_routes.route("/logout", methods=["POST"])
async def logout():
    """تسجيل الخروج عن طريق حذف الكوكي."""
    response = jsonify({"message": "Successfully logged out"})

    # لحذف الكوكي، نرسل نفس الإعدادات مع قيمة فارغة و max_age=0
    settings = get_cookie_settings()
    settings["max_age"] = 0
    response.set_cookie(REFRESH_COOKIE_NAME, "", **settings)

    return response