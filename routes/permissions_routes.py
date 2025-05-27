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
    """جلب سجل العمليات مع فلترة متقدمة"""
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 25))
    offset = (page - 1) * limit
    
    # فلاتر البحث
    user_email = request.args.get("user_email")
    action = request.args.get("action")
    category = request.args.get("category")
    severity = request.args.get("severity")
    resource = request.args.get("resource")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    session_id = request.args.get("session_id")
    
    # بناء الاستعلام
    where_conditions = []
    params = []
    param_count = 0
    
    if user_email:
        param_count += 1
        where_conditions.append(f"al.user_email ILIKE ${param_count}")
        params.append(f"%{user_email}%")
    
    if action:
        param_count += 1
        where_conditions.append(f"al.action ILIKE ${param_count}")
        params.append(f"%{action}%")
    
    if category:
        param_count += 1
        where_conditions.append(f"al.category = ${param_count}")
        params.append(category)
    
    if severity:
        param_count += 1
        where_conditions.append(f"al.severity = ${param_count}")
        params.append(severity)
    
    if resource:
        param_count += 1
        where_conditions.append(f"al.resource = ${param_count}")
        params.append(resource)
    
    if date_from:
        param_count += 1
        where_conditions.append(f"al.created_at >= ${param_count}")
        params.append(date_from)
    
    if date_to:
        param_count += 1
        where_conditions.append(f"al.created_at <= ${param_count}")
        params.append(date_to)
    
    if session_id:
        param_count += 1
        where_conditions.append(f"al.session_id = ${param_count}")
        params.append(session_id)
    
    where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
    
    async with current_app.db_pool.acquire() as connection:
        # استعلام السجلات
        query = f"""
            SELECT 
                al.id, al.user_email, 
                COALESCE(pu.display_name, al.user_email) as user_display_identifier,
                al.action, al.resource, al.resource_id, al.details,
                al.old_values, al.new_values, al.ip_address, al.user_agent,
                al.session_id, al.severity, al.category, al.created_at
            FROM audit_logs al
            LEFT JOIN panel_users pu ON al.user_email = pu.email
            {where_clause}
            ORDER BY al.created_at DESC 
            LIMIT ${param_count + 1} OFFSET ${param_count + 2}
        """
        params.extend([limit, offset])
        
        logs = await connection.fetch(query, *params)
        
        # استعلام العدد الإجمالي
        count_query = f"SELECT COUNT(*) FROM audit_logs al {where_clause}"
        total_count = await connection.fetchval(count_query, *params[:-2])
        
        result_logs = []
        for log in logs:
            log_dict = dict(log)
            # تحويل JSONB إلى dict
            for json_field in ['details', 'old_values', 'new_values']:
                if log_dict[json_field]:
                    try:
                        log_dict[json_field] = json.loads(log_dict[json_field]) if isinstance(log_dict[json_field], str) else log_dict[json_field]
                    except:
                        pass
            
            if log_dict["created_at"]:
                log_dict["created_at"] = log_dict["created_at"].isoformat()
            
            result_logs.append(log_dict)
        
        return jsonify({
            "logs": result_logs,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "pages": (total_count + limit - 1) // limit
            },
            "filters": {
                "user_email": user_email,
                "action": action,
                "category": category,
                "severity": severity,
                "resource": resource,
                "date_from": date_from,
                "date_to": date_to,
                "session_id": session_id
            }
        }), 200

@permissions_routes.route("/audit-logs/summary", methods=["GET"])
@permission_required("system.view_audit_log")
async def get_audit_summary():
    """إحصائيات سجل العمليات"""
    async with current_app.db_pool.acquire() as connection:
        # إحصائيات عامة
        stats = await connection.fetchrow("""
            SELECT 
                COUNT(*) as total_logs,
                COUNT(DISTINCT user_email) as unique_users,
                COUNT(DISTINCT session_id) as unique_sessions,
                MIN(created_at) as earliest_log,
                MAX(created_at) as latest_log
            FROM audit_logs
        """)
        
        # أهم الأنشطة
        top_actions = await connection.fetch("""
            SELECT action, COUNT(*) as count
            FROM audit_logs
            GROUP BY action
            ORDER BY count DESC
            LIMIT 10
        """)
        
        # أكثر المستخدمين نشاطاً
        top_users = await connection.fetch("""
            SELECT 
                al.user_email,
                COALESCE(pu.display_name, al.user_email) as display_name,
                COUNT(*) as activity_count
            FROM audit_logs al
            LEFT JOIN panel_users pu ON al.user_email = pu.email
            GROUP BY al.user_email, pu.display_name
            ORDER BY activity_count DESC
            LIMIT 10
        """)
        
        # إحصائيات حسب التصنيف
        category_stats = await connection.fetch("""
            SELECT 
                COALESCE(category, 'uncategorized') as category,
                COUNT(*) as count
            FROM audit_logs
            GROUP BY category
            ORDER BY count DESC
        """)
        
        # إحصائيات حسب الخطورة
        severity_stats = await connection.fetch("""
            SELECT severity, COUNT(*) as count
            FROM audit_logs
            GROUP BY severity
            ORDER BY 
                CASE severity
                    WHEN 'CRITICAL' THEN 1
                    WHEN 'ERROR' THEN 2
                    WHEN 'WARNING' THEN 3
                    WHEN 'INFO' THEN 4
                    ELSE 5
                END
        """)
        
        return jsonify({
            "summary": dict(stats),
            "top_actions": [dict(row) for row in top_actions],
            "top_users": [dict(row) for row in top_users],
            "category_distribution": [dict(row) for row in category_stats],
            "severity_distribution": [dict(row) for row in severity_stats]
        }), 200
    

