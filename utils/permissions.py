import logging
from functools import wraps
from quart import request, jsonify, current_app
import jwt
from config import SECRET_KEY
import json


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


async def log_action(user_email, action, resource=None, resource_id=None, details=None,
                     ip_address=None, user_agent=None):
    logging.critical("!!! INSIDE CORRECT log_action FUNCTION (EXPERIMENTAL JSON DUMPS) !!!")
    logging.critical(f"Type of details before DB insert: {type(details)}")
    logging.critical(f"Value of details before DB insert: {details}")

    async with current_app.db_pool.acquire() as connection:
        final_ip_address = ip_address
        if final_ip_address is None and request:
            x_forwarded_for = request.headers.get('X-Forwarded-For')
            if x_forwarded_for:
                final_ip_address = x_forwarded_for.split(',')[0].strip()
            else:
                final_ip_address = request.remote_addr

        final_user_agent = user_agent
        if final_user_agent is None and request:
            final_user_agent = request.headers.get('User-Agent')

        # --- بداية التعديل التجريبي ---
        details_for_db_experimental = None
        if isinstance(details, dict):
            try:
                details_for_db_experimental = json.dumps(details) # تحويل القاموس إلى سلسلة JSON
                logging.critical(f"details_for_db_experimental (json string): {details_for_db_experimental}")
            except TypeError as e:
                logging.error(f"Could not serialize details to JSON: {e} - Details: {details}")
                details_for_db_experimental = str(details) # كحل أخير، حوله إلى سلسلة عادية
        elif details is not None:
            details_for_db_experimental = str(details) # إذا لم يكن قاموسًا، حوله إلى سلسلة
        # --- نهاية التعديل التجريبي ---

        logging.critical(f"details_for_db_experimental being passed to execute: {details_for_db_experimental}")
        logging.critical(f"Type of details_for_db_experimental: {type(details_for_db_experimental)}")


        await connection.execute("""
            INSERT INTO audit_logs (user_email, action, resource, resource_id, details, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, user_email, action, resource, resource_id,
             details_for_db_experimental,  # <--- استخدام السلسلة المحولة تجريبيًا
             final_ip_address,
             final_user_agent)
    logging.critical("!!! log_action FINISHED (EXPERIMENTAL JSON DUMPS) !!!")

def permission_required(permission):
    """Decorator للتحقق من صلاحية محددة"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return jsonify({"error": "Authorization header missing"}), 401

            try:
                token = auth_header.split(" ")[1]
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_email = payload.get("email")

                if not await has_permission(user_email, permission):
                    # تسجيل محاولة الوصول غير المصرح بها
                    await log_action(
                        user_email,
                        "UNAUTHORIZED_ACCESS_ATTEMPT",
                        details={"required_permission": permission, "endpoint": request.endpoint}
                    )
                    return jsonify({"error": f"Permission required: {permission}"}), 403

            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401
            except Exception as e:
                logging.error(f"Permission check error: {e}")
                return jsonify({"error": "Permission check failed"}), 500

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def owner_required(func):
    """Decorator للتحقق من صلاحية Owner فقط"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return jsonify({"error": "Authorization header missing"}), 401

        try:
            token = auth_header.split(" ")[1]
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")

            async with current_app.db_pool.acquire() as connection:
                user_role = await connection.fetchval("""
                    SELECT r.name FROM panel_users u
                    JOIN roles r ON u.role_id = r.id
                    WHERE u.email = $1
                """, user_email)

                if user_role != 'owner':
                    await log_action(
                        user_email,
                        "UNAUTHORIZED_OWNER_ACCESS_ATTEMPT",
                        details={"endpoint": request.endpoint}
                    )
                    return jsonify({"error": "Owner privileges required"}), 403

        except Exception as e:
            logging.error(f"Owner check error: {e}")
            return jsonify({"error": "Authorization failed"}), 500

        return await func(*args, **kwargs)

    return wrapper