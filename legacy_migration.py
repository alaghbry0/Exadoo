# seed_data.py
import asyncpg
import asyncio
import logging  # لاستخدام logging بدلاً من print للأخطاء
import os
from dotenv import load_dotenv # تأكد من تثبيت python-dotenv: pip install python-dotenv
load_dotenv()
# إعدادات تسجيل بسيطة
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- تفاصيل الاتصال بقاعدة البيانات ---
# !!! قم بتعديل هذه القيم لتناسب بيئتك المحلية !!!
DB_CONFIG = {
    'user': os.getenv('DB_USER', 'neondb_owner'),
    'password': os.getenv('DB_PASSWORD', 'npg_hqkR5UfFX'),
    'database': os.getenv('DB_NAME', 'neondb'),
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 5432)),
    'ssl': 'require'
}

# --- بيانات الأدوار ---
ROLES_DATA = [
    ('owner', 'مالك النظام - صلاحيات كاملة'),
    ('super_admin', 'مدير عام - صلاحيات واسعة'),
    ('admin', 'مدير - صلاحيات محدودة'),
    ('marketer', 'مسوق - صلاحيات التسويق'),
    ('support', 'دعم فني - صلاحيات الدعم'),
    ('viewer', 'مشاهد - صلاحيات القراءة فقط')
]

# --- بيانات الصلاحيات ---
PERMISSIONS_DATA = [
    # ============== إدارة مستخدمي لوحة التحكم (Panel Users) ==============
    ('panel_users.create', 'إنشاء مستخدمين جدد للوحة التحكم', 'panel_users'),
    ('panel_users.read', 'عرض قائمة مستخدمي لوحة التحكم', 'panel_users'),
    ('panel_users.delete', 'حذف مستخدمي لوحة التحكم', 'panel_users'),
    ('panel_users.manage_roles', 'إدارة أدوار مستخدمي لوحة التحكم', 'panel_users'),

    # ============== إدارة مستخدمي البوت (Bot Users/Subscribers) ==============
    ('bot_users.read', 'عرض قائمة مستخدمي البوت (المشتركين)', 'bot_users'),
    ('bot_users.read_details', 'عرض تفاصيل مستخدم بوت معين (اشتراكات، مدفوعات)', 'bot_users'),
    ('bot_users.export', 'تصدير بيانات مستخدمي البوت', 'bot_users'),

    # ============== إدارة أنواع الاشتراكات (Subscription Types) ==============
    ('subscription_types.create', 'إنشاء أنواع اشتراكات جديدة', 'subscription_types'),
    ('subscription_types.read', 'عرض قائمة أنواع الاشتراكات', 'subscription_types'),
    ('subscription_types.update', 'تعديل أنواع الاشتراكات', 'subscription_types'),
    ('subscription_types.delete', 'حذف أنواع الاشتراكات', 'subscription_types'),

    # ============== إدارة خطط الاشتراك (Subscription Plans) ==============
    ('subscription_plans.create', 'إنشاء خطط اشتراك جديدة', 'subscription_plans'),
    ('subscription_plans.read', 'عرض قائمة خطط الاشتراك', 'subscription_plans'),
    ('subscription_plans.update', 'تعديل خطط الاشتراك', 'subscription_plans'),
    ('subscription_plans.delete', 'حذف خطط الاشتراك', 'subscription_plans'),

    # ============== إدارة اشتراكات المستخدمين (User Subscriptions) ==============
    ('user_subscriptions.create_manual', 'إضافة اشتراك جديد يدويًا للمستخدمين', 'user_subscriptions'),
    ('user_subscriptions.read', 'عرض قائمة اشتراكات المستخدمين', 'user_subscriptions'),
    ('user_subscriptions.update', 'تعديل اشتراكات المستخدمين (تاريخ، خطة، مصدر)', 'user_subscriptions'),
    ('user_subscriptions.cancel', 'إلغاء اشتراكات المستخدمين', 'user_subscriptions'),
    ('user_subscriptions.read_sources', 'عرض مصادر الاشتراكات المتاحة', 'user_subscriptions'),

    # ============== إدارة الاشتراكات المعلقة (Pending Subscriptions) ==============
    ('pending_subscriptions.read', 'عرض قائمة الأعضاء غير المشتركين في القنوات', 'pending_subscriptions'),
    ('pending_subscriptions.stats', 'عرض إحصائيات الأعضاء غير المشتركين', 'pending_subscriptions'),
    ('pending_subscriptions.remove_single', 'إزالة عضو غير مشترك واحد من القنوات', 'pending_subscriptions'),
    ('pending_subscriptions.remove_bulk', 'إزالة جماعية للأعضاء غير المشتركين من القنوات', 'pending_subscriptions'),

    # ============== إدارة الاشتراكات القديمة (Legacy Subscriptions) ==============
    ('legacy_subscriptions.read', 'عرض قائمة الاشتراكات القديمة', 'legacy_subscriptions'),
    ('legacy_subscriptions.stats', 'عرض إحصائيات الاشتراكات القديمة', 'legacy_subscriptions'),

    # ============== إدارة المدفوعات والتحويلات (Payments & Transactions) ==============
    ('payments.read_all', 'عرض جميع سجلات المدفوعات', 'payments'),
    ('payments.read_incoming_transactions', 'عرض التحويلات المالية الواردة للمحفظة', 'payments'),
    ('payments.reports', 'عرض تقارير المدفوعات المالية', 'payments'),

    # ============== إعدادات النظام والتكوين (System Configuration) ==============
    ('system.manage_wallet', 'إدارة عنوان المحفظة و API Key', 'system_config'),
    ('system.manage_reminder_settings', 'إدارة إعدادات رسائل التذكير', 'system_config'),
    ('system.view_settings', 'عرض إعدادات النظام العامة (غير حساسة)', 'system_config'),
    ('system.backup', 'إدارة النسخ الاحتياطية للنظام', 'system_config'),
    ('system.view_logs', 'عرض سجلات النظام (logs)', 'system_config'),
    ('system.view_audit_log', 'عرض سجل تدقيق العمليات', 'system_config'),

    # ============== إدارة الأدوار والصلاحيات (RBAC Management) ==============
    ('roles.create', 'إنشاء أدوار جديدة', 'rbac'),
    ('roles.read', 'عرض الأدوار الموجودة', 'rbac'),
    ('roles.update', 'تعديل الأدوار وصلاحياتها', 'rbac'),
    ('roles.delete', 'حذف الأدوار', 'rbac'),
    ('permissions.manage', 'إدارة الصلاحيات المتاحة في النظام (للمطورين)', 'rbac'),

    # ============== لوحة التحكم الرئيسية والإحصائيات (Dashboard) ==============
    ('dashboard.view_stats', 'عرض إحصائيات لوحة التحكم الرئيسية', 'dashboard'),
    ('dashboard.view_revenue_chart', 'عرض الرسم البياني للإيرادات', 'dashboard'),
    ('dashboard.view_subscriptions_chart', 'عرض الرسم البياني للاشتراكات', 'dashboard'),
    ('dashboard.view_recent_activities', 'عرض أحدث الأنشطة', 'dashboard'),
    ('dashboard.view_recent_payments', 'عرض أحدث المدفوعات', 'dashboard')
]

# --- تعيينات الصلاحيات للأدوار (اسم الدور: [قائمة بأسماء الصلاحيات]) ---
ROLE_PERMISSION_MAPPINGS = {
    'owner': ['ALL'],  # كلمة مفتاحية خاصة لجميع الصلاحيات
    'super_admin': [
        p[0] for p in PERMISSIONS_DATA if p[0] not in [
            'permissions.manage',  # لا يدير الصلاحيات المتاحة
            'roles.delete'  # قد لا يحذف الأدوار إلا إذا كان المالك
        ]
    ],
    'admin': [
        'bot_users.read', 'bot_users.read_details',
        'subscription_types.read',
        'subscription_plans.read',
        'user_subscriptions.read', 'user_subscriptions.update', 'user_subscriptions.create_manual',
        'user_subscriptions.cancel', 'user_subscriptions.read_sources',
        'pending_subscriptions.read', 'pending_subscriptions.stats', 'pending_subscriptions.remove_single',
        'pending_subscriptions.remove_bulk',
        'legacy_subscriptions.read', 'legacy_subscriptions.stats',
        'payments.read_all', 'payments.read_incoming_transactions',
        'system.view_settings', 'system.view_audit_log',
        'dashboard.view_stats', 'dashboard.view_revenue_chart', 'dashboard.view_subscriptions_chart',
        'dashboard.view_recent_activities', 'dashboard.view_recent_payments'
        # ملاحظة: تم تعديل صلاحيات dashboard.view_recent_payments لتكون ضمن صلاحيات admin
    ],
    'marketer': [
        'bot_users.read', 'bot_users.export',
        'subscription_types.read', 'subscription_plans.read',
        'user_subscriptions.read', 'user_subscriptions.read_sources',
        'legacy_subscriptions.read', 'legacy_subscriptions.stats',
        'payments.reports',
        'pending_subscriptions.stats',
        'dashboard.view_stats', 'dashboard.view_revenue_chart', 'dashboard.view_subscriptions_chart'
    ],
    'support': [
        'bot_users.read', 'bot_users.read_details',
        'subscription_types.read', 'subscription_plans.read',
        'user_subscriptions.read', 'user_subscriptions.update', 'user_subscriptions.create_manual',
        'user_subscriptions.cancel', 'user_subscriptions.read_sources',
        'pending_subscriptions.read', 'pending_subscriptions.remove_single',
        'legacy_subscriptions.read',
        'payments.read_all',
        'system.view_audit_log'
    ],
    'viewer': [
        'bot_users.read',
        'subscription_types.read', 'subscription_plans.read',
        'user_subscriptions.read', 'user_subscriptions.read_sources',
        'legacy_subscriptions.read', 'legacy_subscriptions.stats',
        'payments.reports',
        'pending_subscriptions.stats',
        'dashboard.view_stats', 'dashboard.view_revenue_chart', 'dashboard.view_subscriptions_chart',
        'dashboard.view_recent_activities', 'dashboard.view_recent_payments',
        'system.view_audit_log'
    ]
}

# --- قائمة المستخدمين الذين يجب أن يكونوا owner ---
OWNER_EMAILS = [
    'Mmahdy502@gmail.com',
    'mmahdy502@gmail.com',
    'mohammedalaghbry3@gmail.com'
    # يمكنك إضافة المزيد هنا إذا لزم الأمر
]


async def seed_roles(conn):
    logging.info("Seeding roles...")
    try:
        # استخدام ON CONFLICT لتجنب الأخطاء إذا كانت الأدوار موجودة بالفعل
        await conn.executemany(
            "INSERT INTO roles (name, description) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            ROLES_DATA
        )
        logging.info(f"{len(ROLES_DATA)} roles processed.")
    except Exception as e:
        logging.error(f"Error seeding roles: {e}", exc_info=True)


async def seed_permissions(conn):
    logging.info("Seeding permissions...")
    try:
        # استخدام ON CONFLICT لتجنب الأخطاء إذا كانت الصلاحيات موجودة بالفعل
        await conn.executemany(
            "INSERT INTO permissions (name, description, category) VALUES ($1, $2, $3) ON CONFLICT (name) DO NOTHING",
            PERMISSIONS_DATA
        )
        logging.info(f"{len(PERMISSIONS_DATA)} permissions processed.")
    except Exception as e:
        logging.error(f"Error seeding permissions: {e}", exc_info=True)


async def seed_role_permissions(conn):
    logging.info("Seeding role_permissions...")
    try:
        for role_name, perm_names in ROLE_PERMISSION_MAPPINGS.items():
            role_record = await conn.fetchrow("SELECT id FROM roles WHERE name = $1", role_name)
            if not role_record:
                logging.warning(f"Role '{role_name}' not found. Skipping its permissions.")
                continue

            role_id = role_record['id']

            if perm_names == ['ALL']:  # حالة خاصة للمالك
                # جلب جميع IDs الصلاحيات
                all_permission_ids = await conn.fetch("SELECT id FROM permissions")
                permissions_to_assign = [(role_id, p_id['id']) for p_id in all_permission_ids]
            else:
                # جلب IDs الصلاحيات المحددة
                # بناء استعلام IN بشكل آمن
                if not perm_names:  # إذا كانت قائمة الصلاحيات فارغة للدور
                    logging.info(f"No permissions specified for role '{role_name}'. Skipping.")
                    continue

                # تحويل أسماء الصلاحيات إلى IDs
                # بناء قائمة من $1, $2, ...
                placeholders = ', '.join([f'${i + 1}' for i in range(len(perm_names))])
                query = f"SELECT id FROM permissions WHERE name IN ({placeholders})"
                specific_permission_ids_records = await conn.fetch(query, *perm_names)

                permissions_to_assign = [(role_id, p_id['id']) for p_id in specific_permission_ids_records]

            if permissions_to_assign:
                await conn.executemany(
                    "INSERT INTO role_permissions (role_id, permission_id) VALUES ($1, $2) ON CONFLICT (role_id, permission_id) DO NOTHING",
                    permissions_to_assign
                )
                logging.info(f"Assigned {len(permissions_to_assign)} permissions to role '{role_name}'.")
            else:
                logging.info(
                    f"No new permissions to assign to role '{role_name}' (either already exist or none specified/found).")

    except Exception as e:
        logging.error(f"Error seeding role_permissions: {e}", exc_info=True)


async def update_panel_users_roles(conn):
    logging.info("Updating panel_users roles...")
    try:
        # 1. إضافة عمود role_id إذا لم يكن موجودًا (مع التحقق)
        # هذا الجزء يفترض أنك قد تقوم بتشغيل السكربت على قاعدة بيانات لم يتم تعديلها بعد
        # أو أنه آمن لإعادة التشغيل.

        # التحقق من وجود عمود role_id
        role_id_column_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns 
                WHERE table_schema = 'public' 
                AND table_name = 'panel_users' 
                AND column_name = 'role_id'
            );
        """)

        if not role_id_column_exists:
            logging.info("Column 'role_id' not found in 'panel_users'. Adding it.")
            await conn.execute("ALTER TABLE panel_users ADD COLUMN role_id INTEGER")
            await conn.execute("""
                ALTER TABLE panel_users ADD CONSTRAINT fk_user_role 
                FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL
            """)
            logging.info("Column 'role_id' and foreign key constraint added.")
        else:
            logging.info("Column 'role_id' already exists in 'panel_users'.")

        # 2. جلب IDs الأدوار 'owner' و 'admin'
        owner_role_id_rec = await conn.fetchrow("SELECT id FROM roles WHERE name = 'owner'")
        admin_role_id_rec = await conn.fetchrow("SELECT id FROM roles WHERE name = 'admin'")

        if not owner_role_id_rec or not admin_role_id_rec:
            logging.error("Critical: 'owner' or 'admin' role not found in 'roles' table. Cannot update panel_users.")
            return

        owner_role_id = owner_role_id_rec['id']
        admin_role_id = admin_role_id_rec['id']

        # 3. تحديث المستخدمين المحددين ليكونوا owner
        # تحويل قائمة الإيميلات إلى حروف صغيرة للمقارنة غير الحساسة لحالة الأحرف
        owner_emails_lower = [email.lower() for email in OWNER_EMAILS]

        # بناء استعلام IN بشكل آمن
        placeholders_owner_emails = ', '.join([f'${i + 1}' for i in range(len(owner_emails_lower))])

        if owner_emails_lower:  # فقط إذا كانت هناك إيميلات محددة للمالك
            update_owners_query = f"UPDATE panel_users SET role_id = {owner_role_id} WHERE lower(email) IN ({placeholders_owner_emails})"
            status_owners = await conn.execute(update_owners_query, *owner_emails_lower)
            logging.info(f"Updated specific users to 'owner' role. Status: {status_owners}")
        else:
            logging.info(
                "No specific emails provided to be set as 'owner'. Skipping direct owner assignment by email list.")

        # 4. تحديث جميع المستخدمين الآخرين (الذين ليسوا في قائمة OWNER_EMAILS) ليكونوا admin
        # أو الذين كان دورهم النصي القديم 'admin'
        # أو الذين ليس لديهم role_id بعد

        # تحديث المستخدمين الذين كان دورهم النصي 'admin' ولم يتم تعيينهم كـ owner
        # بناء استعلام NOT IN بشكل آمن
        if owner_emails_lower:
            update_admins_query_by_old_role = f"""
                UPDATE panel_users
                SET role_id = {admin_role_id}
                WHERE lower(role) = 'admin' AND lower(email) NOT IN ({placeholders_owner_emails}) AND role_id IS NULL
            """
            status_admins_old_role = await conn.execute(update_admins_query_by_old_role, *owner_emails_lower)
        else:  # إذا لم تكن هناك قائمة owner_emails، قم بتحديث جميع من كان دورهم admin
            update_admins_query_by_old_role = f"""
                UPDATE panel_users
                SET role_id = {admin_role_id}
                WHERE lower(role) = 'admin' AND role_id IS NULL
            """
            status_admins_old_role = await conn.execute(update_admins_query_by_old_role)
        logging.info(f"Updated users with old role 'admin' to new 'admin' role_id. Status: {status_admins_old_role}")

        # تحديث أي مستخدم متبقي ليس لديه role_id (وليس من قائمة الـ owners) إلى admin كإجراء احتياطي
        if owner_emails_lower:
            update_remaining_to_admin_query = f"""
                UPDATE panel_users
                SET role_id = {admin_role_id}
                WHERE role_id IS NULL AND lower(email) NOT IN ({placeholders_owner_emails})
            """
            status_remaining_admins = await conn.execute(update_remaining_to_admin_query, *owner_emails_lower)
        else:
            update_remaining_to_admin_query = f"""
                UPDATE panel_users
                SET role_id = {admin_role_id}
                WHERE role_id IS NULL
            """
            status_remaining_admins = await conn.execute(update_remaining_to_admin_query)

        logging.info(f"Updated remaining users without role_id to 'admin'. Status: {status_remaining_admins}")

        # 5. (اختياري) إزالة قيد panel_users_role_check القديم إذا كان لا يزال موجودًا
        # وعمود role القديم بعد التأكد من نجاح الترحيل
        try:
            await conn.execute("ALTER TABLE panel_users DROP CONSTRAINT IF EXISTS panel_users_role_check")
            logging.info("Dropped old 'panel_users_role_check' constraint if it existed.")
            # يمكنك إضافة ALTER TABLE panel_users DROP COLUMN role; هنا إذا كنت واثقًا
            # لكن من الأفضل القيام بذلك يدويًا بعد التحقق.
        except Exception as e_alter:
            logging.warning(f"Could not drop old constraint/column (might not exist or other issue): {e_alter}")


    except Exception as e:
        logging.error(f"Error updating panel_users roles: {e}", exc_info=True)


async def main():
    conn = None
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
        logging.info("Successfully connected to the database.")

        # ترتيب التنفيذ مهم
        await seed_roles(conn)
        await seed_permissions(conn)
        await seed_role_permissions(conn)

        # تحديث المستخدمين الحاليين بعد إنشاء الأدوار
        await update_panel_users_roles(conn)

        logging.info("Seeding and user update process completed successfully!")

    except asyncpg.exceptions.InvalidPasswordError:
        logging.error("Database connection failed: Invalid password. Please check DB_CONFIG.")
    except asyncpg.exceptions.CannotConnectNowError:
        logging.error("Database connection failed: Cannot connect to the server. Is it running and accessible?")
    except Exception as e:
        logging.error(f"An unexpected error occurred during the main process: {e}", exc_info=True)
    finally:
        if conn:
            await conn.close()
            logging.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())