import jwt
import datetime
from google.oauth2 import id_token
from google.auth.transport import requests
from config import GOOGLE_CLIENT_ID, SECRET_KEY
from quart import request, jsonify, abort

async def verify_google_token(token):
    """تحقق من صحة توكن Google OAuth"""
    try:
        payload = id_token.verify_oauth2_token(token, requests.Request(), GOOGLE_CLIENT_ID)
        if "email" not in payload:
            abort(400, description="Invalid ID Token: No email found")
        return {"email": payload["email"], "display_name": payload.get("name", "")}
    except ValueError as e:
        abort(400, description=f"Invalid ID Token: {str(e)}")
    except Exception as e:
        abort(400, description=f"Token verification failed: {str(e)}")

def create_jwt(email, role):
    """إنشاء JSON Web Token (JWT) يحتوي على البريد الإلكتروني والدور"""
    payload = {
        "email": email,
        "role": role,
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

async def get_current_user():
    """استخراج بيانات المستخدم من توكن الـ JWT وإرجاع بيانات المستخدم مع الدور""" # تم التعديل في الوصف
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        abort(401, description="Authorization header missing")

    try:
        token = auth_header.split(" ")[1]  # Bearer <token>
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return {"email": payload["email"], "role": payload["role"]} # تم التعديل: إرجاع role المستخدم
    except IndexError:
        abort(401, description="Invalid Authorization header format")
    except jwt.ExpiredSignatureError:
        abort(401, description="Token expired")
    except jwt.InvalidTokenError:
        abort(401, description="Invalid token")

def admin_required(func):
    """Decorator للتحقق من صلاحية المستخدم (admin أو owner)"""
    async def wrapper(*args, **kwargs):
        user = await get_current_user()
        if user and user["role"] in ["admin", "owner"]: # تم التعديل: التحقق من أن الدور هو admin أو owner
            return await func(*args, **kwargs)
        else:
            abort(403, description="Admin or Owner role required") # تم التعديل في رسالة الخطأ
    return wrapper

def owner_required(func):
    """Decorator للتحقق من صلاحية المستخدم (owner فقط)"""
    async def wrapper(*args, **kwargs):
        user = await get_current_user()
        if user and user["role"] == "owner": # تم التعديل: التحقق من أن الدور هو owner فقط
            return await func(*args, **kwargs)
        else:
            abort(403, description="Owner role required") # تم التعديل في رسالة الخطأ
    return wrapper