from quart import Blueprint, request, jsonify, current_app, abort
from chatbot.knowledge_base import KnowledgeBase
import jwt
import json
from functools import wraps
from auth import get_current_user
from config import SECRET_KEY
import pytz



admin_chatbot_bp = Blueprint('admin_chatbot', __name__)
knowledge_base = KnowledgeBase()

def role_required(required_role):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return jsonify({"error": "Authorization header missing"}), 401
            try:
                token = auth_header.split(" ")[1]  # Bearer <token>
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                user_role = payload.get("role")
                # يمكنك السماح للمالك أيضًا بالقيام بإجراءات الأدمن إذا رغبت
                if required_role == "admin" and user_role not in ["admin", "owner"]:
                    return jsonify({"error": "Admin privileges required"}), 403
                elif required_role == "owner" and user_role != "owner":
                    return jsonify({"error": "Owner privileges required"}), 403
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401

            return await func(*args, **kwargs)

        return wrapper

    return decorator



@admin_chatbot_bp.route('/settings', methods=['GET'])
@role_required("admin")
async def get_settings():
    """الحصول على إعدادات البوت الحالية"""
    try:
        async with current_app.db_pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM bot_settings ORDER BY id DESC LIMIT 1')

            if row:
                return jsonify(dict(row))
            else:
                return jsonify({
                    'name': 'دعم عملاء اكسادوا',
                    'prompt_template': 'أنت مساعد دعم العملاء لشركة اكسادوا. {context}',
                    'welcome_message': 'مرحباً بك! كيف يمكنني مساعدتك اليوم؟',
                    'fallback_message': 'آسف، لا يمكنني الإجابة على هذا السؤال. هل يمكنني مساعدتك بشيء آخر؟',
                    'model_settings': {'temperature': 0.7, 'max_tokens': 500}
                })

    except Exception as e:
        current_app.logger.error(f"خطأ في الحصول على إعدادات البوت: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء استرجاع الإعدادات'}), 500


@admin_chatbot_bp.route('/settings', methods=['POST'])
@role_required("admin")
async def update_settings():
    data = await request.get_json()
    # ... تحقق من الحقول المطلوبة
    model_settings = data.get('model_settings', {'temperature': 0.7, 'max_tokens': 500})
    model_settings_json = json.dumps(model_settings)   # 🚩 هنا

    async with current_app.db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_settings 
            (name, prompt_template, welcome_message, fallback_message, model_settings)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            data['name'],
            data['prompt_template'],
            data['welcome_message'],
            data['fallback_message'],
            model_settings_json
        )
    return jsonify({'status': 'success'})


@admin_chatbot_bp.route('/knowledge', methods=['GET'])
@role_required("admin")
async def list_knowledge():
    """الحصول على قائمة عناصر قاعدة المعرفة"""
    try:
        category = request.args.get('category')
        query = request.args.get('query')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))

        offset = (page - 1) * per_page

        # بناء استعلام ديناميكي
        where_clauses = []
        params = []
        param_index = 1

        if category:
            where_clauses.append(f"category = ${param_index}")
            params.append(category)
            param_index += 1

        if query:
            where_clauses.append(f"(title ILIKE ${param_index} OR content ILIKE ${param_index})")
            params.append(f"%{query}%")
            param_index += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # إضافة ترتيب الصفحات
        params.extend([per_page, offset])

        async with current_app.db_pool.acquire() as conn:
            # الحصول على إجمالي العدد
            count_sql = f"SELECT COUNT(*) FROM knowledge_base WHERE {where_sql}"
            total = await conn.fetchval(count_sql, *params[:-2]) if params else await conn.fetchval(count_sql)

            # الحصول على البيانات
            data_sql = f"""
                SELECT id, title, category, tags, created_at, updated_at
                FROM knowledge_base 
                WHERE {where_sql}
                ORDER BY updated_at DESC
                LIMIT ${param_index} OFFSET ${param_index + 1}
            """
            rows = await conn.fetch(data_sql, *params)

            items = [dict(row) for row in rows]

            return jsonify({
                'items': items,
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': (total + per_page - 1) // per_page
            })

    except Exception as e:
        current_app.logger.error(f"خطأ في استرجاع قاعدة المعرفة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء استرجاع قاعدة المعرفة'}), 500


@admin_chatbot_bp.route('/knowledge/<int:item_id>', methods=['GET'])
@role_required("admin")
async def get_knowledge_item(item_id):
    """الحصول على عنصر محدد من قاعدة المعرفة"""
    try:
        async with current_app.db_pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM knowledge_base WHERE id = $1', item_id)

            if row:
                return jsonify(dict(row))
            else:
                return jsonify({'error': 'العنصر غير موجود'}), 404

    except Exception as e:
        current_app.logger.error(f"خطأ في استرجاع عنصر قاعدة المعرفة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء استرجاع العنصر'}), 500


@admin_chatbot_bp.route('/knowledge', methods=['POST'])
@role_required("admin")
async def add_knowledge_item():
    """إضافة عنصر جديد إلى قاعدة المعرفة"""
    try:
        data = await request.get_json()

        if not data.get('title') or not data.get('content'):
            return jsonify({'error': 'العنوان والمحتوى مطلوبان'}), 400

        item_id = await knowledge_base.add_item(
            data['title'],
            data['content'],
            data.get('category'),
            data.get('tags', [])
        )

        return jsonify({'id': item_id, 'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"خطأ في إضافة عنصر قاعدة المعرفة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء إضافة العنصر'}), 500


@admin_chatbot_bp.route('/knowledge/<int:item_id>', methods=['PUT'])
@role_required("admin")
async def update_knowledge_item(item_id):
    """تحديث عنصر موجود في قاعدة المعرفة"""
    try:
        data = await request.get_json()

        success = await knowledge_base.update_item(
            item_id,
            data.get('title'),
            data.get('content'),
            data.get('category'),
            data.get('tags')
        )

        if success:
            return jsonify({'status': 'success'})
        else:
            return jsonify({'error': 'لم يتم تحديث أي بيانات'}), 400

    except Exception as e:
        current_app.logger.error(f"خطأ في تحديث عنصر قاعدة المعرفة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء تحديث العنصر'}), 500


@admin_chatbot_bp.route('/knowledge/<int:item_id>', methods=['DELETE'])
@role_required("admin")
async def delete_knowledge_item(item_id):
    """حذف عنصر من قاعدة المعرفة"""
    try:
        success = await knowledge_base.delete_item(item_id)

        if success:
            return jsonify({'status': 'success'})
        else:
            return jsonify({'error': 'لم يتم العثور على العنصر'}), 404

    except Exception as e:
        current_app.logger.error(f"خطأ في حذف عنصر قاعدة المعرفة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء حذف العنصر'}), 500


@admin_chatbot_bp.route('/rebuild-embeddings', methods=['POST'])
@role_required("admin")
async def rebuild_embeddings():
    """إعادة بناء embeddings لكل قاعدة المعرفة"""
    try:
        # التحقق من مصادقة المستخدم (يمكن إضافة منطق المصادقة هنا)

        # إعادة بناء embeddings
        updated_count = await knowledge_base.rebuild_embeddings()

        return jsonify({
            'status': 'success',
            'message': f'تم تحديث embeddings لـ {updated_count} عنصر.'
        })

    except Exception as e:
        current_app.logger.error(f"خطأ في إعادة بناء embeddings: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء إعادة بناء embeddings'}), 500
