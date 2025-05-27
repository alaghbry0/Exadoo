import logging
from functools import wraps
from quart import request, jsonify, current_app
import jwt
from config import SECRET_KEY
from uuid import uuid

from utils.audit_logger import audit_logger, AuditCategory, AuditSeverity
from auth import get_current_user

async def get_user_permissions(email):
    """جلب صلاحيات المستخدم من قاعدة البيانات"""
    async with current_app.db_pool.acquire() as connection:
        result = await connection.fetch("""
            SELECT DISTINCT p.name
            FROM panel_users u
            JOIN roles r ON u.role_id = r.id
            JOIN role_permissions rp ON r.id = rp.role_id
            JOIN permissions p ON rp.permission_id = p.id
            WHERE u.email = $1
        """, email)
        return [row['name'] for row in result]


async def has_permission(email, permission):
    """فحص صلاحية محددة للمستخدم"""
    permissions = await get_user_permissions(email)
    return permission in permissions

def audit_action(
    action: str,
    category: AuditCategory = None,
    severity: AuditSeverity = AuditSeverity.INFO,
    resource_type: str = None,
    track_changes: bool = False # هذا الوسيط يمكن أن يكون مفيدًا كعلامة, لكن الديكور نفسه لا يستخدمه حاليًا لتتبع القيم القديمة/الجديدة تلقائيًا
):
    """Decorator لتسجيل العمليات تلقائياً"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user = await get_current_user()
            user_email = user.get("email") if user else "system"
            
            current_session_id = audit_logger.session_id # المحاولة للحصول على session_id موجود
            if not current_session_id: # إذا لم يكن هناك session_id من سياق أعلى
                current_session_id = str(uuid.uuid4()) # إنشاء واحد جديد
                audit_logger.set_session_id(current_session_id)
            
            details = {
                "endpoint": request.endpoint,
                "method": request.method,
                "function": func.__name__
            }
            
            if request.method in ['POST', 'PUT', 'PATCH']:
                try:
                    request_data = await request.get_json(silent=True) # silent=True لتجنب الخطأ إذا لم يكن JSON
                    if request_data:
                        details["request_data"] = request_data
                except Exception: # تجاهل أخطاء تحليل JSON هنا، الهدف هو تسجيل العملية
                    logging.debug("Could not parse request data as JSON for audit details.")
                    pass
            
            try:
                result = await func(*args, **kwargs)
                
                # استخراج resource_id إذا كان متاحًا من kwargs أو result
                # هذا يعتمد على كيفية تسمية معاملات المسار أو شكل الاستجابة
                resource_id_val = None
                if 'plan_id' in kwargs: # مثال لـ plan_id
                    resource_id_val = str(kwargs['plan_id'])
                elif isinstance(result, tuple) and len(result) > 0 and isinstance(result[0], dict) and 'id' in result[0]: # إذا كانت الاستجابة JSON وفيها id
                     resource_id_val = str(result[0].get('id'))
                elif isinstance(result, dict) and 'id' in result: # إذا كانت الاستجابة قاموس مباشر
                     resource_id_val = str(result.get('id'))


                await audit_logger.log_action(
                    user_email=user_email,
                    action=f"{action}_SUCCESS",
                    category=category,
                    severity=severity, # استخدام الشدة المعطاة للنجاح
                    resource=resource_type,
                    resource_id=resource_id_val, # محاولة إضافة resource_id
                    details=details,
                    session_id=current_session_id # استخدام نفس session_id
                )
                
                return result
                
            except Exception as e:
                error_details = details.copy()
                error_details["error"] = str(e)
                error_details["error_type"] = type(e).__name__
                
                await audit_logger.log_action(
                    user_email=user_email,
                    action=f"{action}_FAILED",
                    category=category,
                    severity=AuditSeverity.ERROR, # الخطأ دائمًا ERROR أو أعلى
                    resource=resource_type,
                    details=error_details,
                    session_id=current_session_id # استخدام نفس session_id
                )
                raise
        return wrapper
    return decorator

def permission_required(permission):
    """Decorator للتحقق من صلاحية محددة"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # محاولة الحصول على المستخدم الحالي أولاً لتجنب فك التشفير المتكرر
            user = await get_current_user() 
            if not user:
                auth_header = request.headers.get("Authorization")
                if not auth_header:
                    return jsonify({"error": "Authorization header missing"}), 401
                try:
                    token = auth_header.split(" ")[1]
                    payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                    user_email = payload.get("email")
                    if not user_email: # تأكد من وجود الإيميل في الحمولة
                         return jsonify({"error": "User email not in token"}), 401
                except jwt.ExpiredSignatureError:
                    return jsonify({"error": "Token expired"}), 401
                except jwt.InvalidTokenError:
                    return jsonify({"error": "Invalid token"}), 401
                except Exception as e:
                    logging.error(f"Token decoding error: {e}")
                    return jsonify({"error": "Authorization processing failed"}), 401
            else:
                user_email = user.get("email")

            if not await has_permission(user_email, permission):
                await audit_logger.log_action( # <--- استخدام audit_logger العالمي
                    user_email=user_email,
                    action="UNAUTHORIZED_ACCESS_ATTEMPT",
                    category=AuditCategory.SECURITY,
                    severity=AuditSeverity.WARNING,
                    resource=request.endpoint, # يمكن استخدام request.path أو request.endpoint
                    details={"required_permission": permission, "endpoint": request.endpoint}
                )
                return jsonify({"error": f"Permission required: {permission}"}), 403
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator


def owner_required(func):
    """Decorator للتحقق من صلاحية Owner فقط"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        user = await get_current_user()
        if not user:
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return jsonify({"error": "Authorization header missing"}), 401
            try:
                token = auth_header.split(" ")[1]
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_email = payload.get("email")
                if not user_email:
                     return jsonify({"error": "User email not in token"}), 401
            except Exception as e: # يشمل ExpiredSignatureError, InvalidTokenError
                logging.error(f"Owner check token error: {e}")
                return jsonify({"error": "Authorization failed"}), 401
        else:
            user_email = user.get("email")

        async with current_app.db_pool.acquire() as connection:
            user_role = await connection.fetchval("""
                SELECT r.name FROM panel_users u
                JOIN roles r ON u.role_id = r.id
                WHERE u.email = $1
            """, user_email)

            if user_role != 'owner':
                await audit_logger.log_action( # <--- استخدام audit_logger العالمي
                    user_email=user_email,
                    action="UNAUTHORIZED_OWNER_ACCESS_ATTEMPT",
                    category=AuditCategory.SECURITY,
                    severity=AuditSeverity.WARNING,
                    resource=request.endpoint,
                    details={"current_role": user_role, "endpoint": request.endpoint}
                )
                return jsonify({"error": "Owner privileges required"}), 403

        return await func(*args, **kwargs)
    return wrapper