from quart import Blueprint, request, jsonify, current_app
from utils.permissions import permission_required, owner_required, log_action, get_user_permissions
from auth import get_current_user
import logging
import json

permissions_routes = Blueprint("permissions_routes", __name__, url_prefix="/api/permissions")


@permissions_routes.route("/roles", methods=["GET"])
@permission_required("roles.read")
async def get_roles():
    """جلب جميع الأدوار"""
    async with current_app.db_pool.acquire() as connection:
        roles_data = await connection.fetch("""
            SELECT r.id,
                   r.name,
                   r.description,
                   r.created_at,
                   r.updated_at,
                   COUNT(DISTINCT rp.permission_id) as permissions_count,
                   COUNT(DISTINCT u.id) as users_count
            FROM roles r
            LEFT JOIN role_permissions rp ON r.id = rp.role_id
            LEFT JOIN panel_users u ON r.id = u.role_id
            GROUP BY r.id, r.name, r.description, r.created_at, r.updated_at
            ORDER BY r.name
        """) # تم تغيير اسم المتغير إلى roles_data لتجنب الخلط مع اسم الجدول

        result = []
        for role_record in roles_data: # تم تغيير اسم المتغير لتجنب الخلط
            result.append({
                "id": role_record["id"],
                "name": role_record["name"],
                "description": role_record["description"],
                "permissions_count": role_record["permissions_count"],
                "users_count": role_record["users_count"],
                "created_at": role_record["created_at"].isoformat() if role_record["created_at"] else None
            })

        return jsonify({"roles": result}), 200


@permissions_routes.route("/permissions", methods=["GET"])
@permission_required("roles.read")
async def get_permissions():
    """جلب جميع الصلاحيات"""
    async with current_app.db_pool.acquire() as connection:
        permissions = await connection.fetch("""
            SELECT * FROM permissions ORDER BY category, name
        """)

        # تجميع الصلاحيات حسب الفئة
        result = {}
        for perm in permissions:
            category = perm["category"] or "other"
            if category not in result:
                result[category] = []
            result[category].append({
                "id": perm["id"],
                "name": perm["name"],
                "description": perm["description"]
            })

        return jsonify({"permissions": result}), 200


@permissions_routes.route("/roles/<int:role_id>/permissions", methods=["GET"])
@permission_required("roles.read")
async def get_role_permissions(role_id):
    """جلب صلاحيات دور محدد"""
    async with current_app.db_pool.acquire() as connection:
        permissions = await connection.fetch("""
            SELECT p.id, p.name, p.description, p.category
            FROM permissions p
            JOIN role_permissions rp ON p.id = rp.permission_id
            WHERE rp.role_id = $1
            ORDER BY p.category, p.name
        """, role_id)

        result = []
        for perm in permissions:
            result.append({
                "id": perm["id"],
                "name": perm["name"],
                "description": perm["description"],
                "category": perm["category"]
            })

        return jsonify({"permissions": result}), 200


@permissions_routes.route("/roles/<int:role_id>/permissions", methods=["PUT"])
@owner_required
async def update_role_permissions(role_id):
    """تحديث صلاحيات دور محدد (Owner فقط)"""
    data = await request.get_json()
    permission_ids = data.get("permission_ids", [])

    user = await get_current_user()

    async with current_app.db_pool.acquire() as connection:
        async with connection.transaction():
            # حذف الصلاحيات الحالية
            await connection.execute("""
                DELETE FROM role_permissions WHERE role_id = $1
            """, role_id)

            # إضافة الصلاحيات الجديدة
            if permission_ids:
                await connection.executemany("""
                    INSERT INTO role_permissions (role_id, permission_id) VALUES ($1, $2)
                """, [(role_id, pid) for pid in permission_ids])

            # جلب اسم الدور للتسجيل
            role_name = await connection.fetchval("""
                SELECT name FROM roles WHERE id = $1
            """, role_id)

            # تسجيل العملية
            await log_action(
                user["email"],
                "UPDATE_ROLE_PERMISSIONS",
                "role",
                str(role_id),
                {
                    "role_name": role_name,
                    "permission_ids": permission_ids,
                    "permissions_count": len(permission_ids)
                }
            )

    return jsonify({"message": "Role permissions updated successfully"}), 200


@permissions_routes.route("/roles", methods=["POST"])
@owner_required
async def create_role():
    """إنشاء دور جديد (Owner فقط)"""
    data = await request.get_json()
    name = data.get("name")
    description = data.get("description", "")
    permission_ids = data.get("permission_ids", [])

    if not name:
        return jsonify({"error": "Role name is required"}), 400

    user = await get_current_user()

    async with current_app.db_pool.acquire() as connection:
        async with connection.transaction():
            # التحقق من عدم وجود دور بنفس الاسم
            existing = await connection.fetchval("""
                SELECT id FROM roles WHERE name = $1
            """, name)

            if existing:
                return jsonify({"error": "Role name already exists"}), 400

            # إنشاء الدور
            role_id = await connection.fetchval("""
                INSERT INTO roles (name, description) VALUES ($1, $2) RETURNING id
            """, name, description)

            # إضافة الصلاحيات
            if permission_ids:
                await connection.executemany("""
                    INSERT INTO role_permissions (role_id, permission_id) VALUES ($1, $2)
                """, [(role_id, pid) for pid in permission_ids])

            # تسجيل العملية
            await log_action(
                user["email"],
                "CREATE_ROLE",
                "role",
                str(role_id),
                {
                    "role_name": name,
                    "description": description,
                    "permission_ids": permission_ids
                }
            )

    return jsonify({"message": "Role created successfully", "role_id": role_id}), 201


@permissions_routes.route("/users/<int:user_id>/role", methods=["PUT"])
@owner_required
@permission_required("roles.update")
async def update_user_role(target_user_id: int):  # استقبال target_user_id من المسار
    """تحديث دور مستخدم محدد."""
    data = await request.get_json()
    new_role_id = data.get("role_id")

    if new_role_id is None:  # التحقق من أن role_id موجود وليس فارغاً (0 يعتبر قيمة صالحة إذا كان معرفاً)
        return jsonify({"error": "role_id is required in the request body"}), 400

    try:
        new_role_id = int(new_role_id)  # تأكد من أنه رقم صحيح
    except ValueError:
        return jsonify({"error": "role_id must be an integer"}), 400

    current_user_performing_action = await get_current_user()

    async with current_app.db_pool.acquire() as connection:
        async with connection.transaction():
            # 1. التحقق من وجود المستخدم المستهدف
            user_to_update = await connection.fetchrow(
                "SELECT id, email, role_id FROM panel_users WHERE id = $1",
                target_user_id
            )

            if not user_to_update:
                return jsonify({"error": "Target user not found"}), 404

            # 2. التحقق من وجود الدور الجديد
            new_role_details = await connection.fetchrow(
                "SELECT id, name FROM roles WHERE id = $1",
                new_role_id
            )

            if not new_role_details:
                return jsonify({"error": "New role not found"}), 404

            # 3. (اختياري ولكن مهم) منع تعديل دور الـ "owner" إلا بواسطة "owner" آخر،
            #    ومنع تعيين دور "owner" إلا بواسطة "owner" آخر.
            #    ومنع المستخدم من تعديل دوره بنفسه ليصبح owner إذا لم يكن owner.

            current_user_role_name = await connection.fetchval("""
                SELECT r.name FROM panel_users u
                JOIN roles r ON u.role_id = r.id
                WHERE u.email = $1
            """, current_user_performing_action["email"])

            target_user_current_role_name = await connection.fetchval("""
                SELECT r.name FROM panel_users u
                JOIN roles r ON u.role_id = r.id
                WHERE u.id = $1
            """, target_user_id)

            # إذا كان الدور الجديد هو 'owner'
            if new_role_details['name'] == 'owner' and current_user_role_name != 'owner':
                await log_action(
                    current_user_performing_action["email"],
                    "UNAUTHORIZED_ASSIGN_OWNER_ROLE_ATTEMPT",
                    resource="user",
                    resource_id=str(target_user_id),
                    details={
                        "target_user_email": user_to_update["email"],
                        "attempted_role_id": new_role_id,
                        "attempted_role_name": new_role_details['name']
                    }
                )
                return jsonify({"error": "Only an owner can assign the owner role."}), 403

            # إذا كان المستخدم المستهدف هو 'owner'
            if target_user_current_role_name == 'owner' and current_user_role_name != 'owner':
                await log_action(
                    current_user_performing_action["email"],
                    "UNAUTHORIZED_MODIFY_OWNER_ROLE_ATTEMPT",
                    resource="user",
                    resource_id=str(target_user_id),
                    details={
                        "target_user_email": user_to_update["email"],
                        "attempted_new_role_id": new_role_id,
                        "attempted_new_role_name": new_role_details['name']
                    }
                )
                return jsonify({"error": "Only an owner can change the role of another owner."}), 403

            # 4. (اختياري) منع المستخدم من تغيير دوره بنفسه إذا كان هذا سيؤدي لترقية صلاحياته بشكل غير مسموح
            #    (مثلاً، admin يحاول تغيير دوره لـ owner). الـ Decorator يجب أن يغطي معظم هذا.

            # 5. تحديث دور المستخدم
            await connection.execute(
                """UPDATE panel_users SET role_id = $1, updated_at = CURRENT_TIMESTAMP
                   WHERE id = $2""",
                new_role_id, target_user_id
            )

            # 6. تسجيل العملية
            await log_action(
                user_email=current_user_performing_action["email"],
                action="UPDATE_USER_ROLE",
                resource="user",
                resource_id=str(target_user_id),  # استخدام ID المستخدم المستهدف
                details={
                    "target_user_email": user_to_update["email"],
                    "previous_role_id": user_to_update["role_id"],
                    # "previous_role_name": target_user_current_role_name, # يمكنك جلب اسم الدور القديم إذا أردت
                    "new_role_id": new_role_id,
                    "new_role_name": new_role_details['name']  # اسم الدور الجديد
                }
            )

    return jsonify(
        {"message": f"Role for user {user_to_update['email']} updated successfully to {new_role_details['name']}"}), 200


@permissions_routes.route("/my-permissions", methods=["GET"])
@permission_required("bot_users.read")
async def get_my_permissions():
    """جلب صلاحيات المستخدم الحالي"""
    user = await get_current_user()
    permissions = await get_user_permissions(user["email"])

    return jsonify({"permissions": permissions}), 200


@permissions_routes.route("/audit-logs", methods=["GET"])
@permission_required("system.view_audit_log")
async def get_audit_logs():
    """جلب سجل العمليات"""
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 25))
    offset = (page - 1) * limit

    async with current_app.db_pool.acquire() as connection:
        logs_from_db = await connection.fetch("""
            SELECT 
                al.id, 
                al.user_email, 
                COALESCE(pu.display_name, al.user_email) as user_display_identifier, -- استخدم display_name، وإذا لم يوجد، استخدم email
                al.action, 
                al.resource, 
                al.resource_id, 
                al.details, 
                al.ip_address, 
                al.user_agent, 
                al.created_at 
            FROM audit_logs al
            LEFT JOIN panel_users pu ON al.user_email = pu.email -- الربط باستخدام البريد الإلكتروني
            ORDER BY al.created_at DESC 
            LIMIT $1 OFFSET $2
        """, limit, offset)

        total_count = await connection.fetchval("SELECT COUNT(*) FROM audit_logs")

        result_logs = []
        for db_log_row in logs_from_db:
            result_logs.append({
                "id": db_log_row["id"],
                "user_email": db_log_row["user_email"], # يمكنك إبقاؤه إذا كنت لا تزال تحتاجه
                "user_display_identifier": db_log_row["user_display_identifier"], # هذا هو الاسم المعروض أو البريد الإلكتروني
                "action": db_log_row["action"],
                "resource": db_log_row["resource"],
                "resource_id": db_log_row["resource_id"],
                "details": db_log_row["details"],
                "ip_address": str(db_log_row["ip_address"]) if db_log_row["ip_address"] else None,
                "user_agent": db_log_row["user_agent"],
                "created_at": db_log_row["created_at"].isoformat() if db_log_row["created_at"] else None
            })

        return jsonify({
            "logs": result_logs,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0
            }
        }), 200