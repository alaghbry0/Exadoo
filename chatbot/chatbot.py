from quart import Blueprint, request, jsonify, current_app
from chatbot.knowledge_base import KnowledgeBase
from chatbot.ai_service import DeepSeekService
from chatbot.chat_manager import ChatManager
import asyncio
import uuid
import re
import json
chatbot_bp = Blueprint('chatbot', __name__)
knowledge_base = KnowledgeBase()
ai_service = DeepSeekService()
chat_manager = ChatManager()


@chatbot_bp.route('/chat', methods=['POST'])
async def chat():
    """تلقي رسالة المستخدم وإرسال الرد"""
    try:
        data = await request.get_json()

        # التحقق من البيانات المدخلة
        if not data:
            return jsonify({'error': 'البيانات غير صالحة'}), 400

        user_message = data.get('message')
        if not user_message or not isinstance(user_message, str):
            return jsonify({'error': 'يجب تقديم رسالة صالحة'}), 400

        # تنظيف رسالة المستخدم لمنع هجمات الحقن
        user_message = _sanitize_input(user_message)

        session_id = data.get('session_id', str(uuid.uuid4()))
        user_id = data.get('user_id', 'anonymous')

        # تنظيف معرفات المستخدم والجلسة
        session_id = _sanitize_input(session_id)
        user_id = _sanitize_input(user_id)

        # الحصول على سجل المحادثات السابقة للسياق
        conversation_history = await chat_manager.get_conversation_history(session_id, limit=3)

        # البحث في قاعدة المعرفة باستخدام embeddings
        relevant_knowledge = await knowledge_base.search(user_message)

        if not relevant_knowledge:
            return jsonify({
                'response': 'عذرًا، لا تتوفر معلومات كافية للإجابة على سؤالك.',
                'session_id': session_id
            })

        # الحصول على إعدادات البوت من قاعدة البيانات
        bot_settings = await _get_bot_settings()
        current_app.logger.debug(f"نوع model_settings: {type(bot_settings.get('model_settings'))}")
        current_app.logger.debug(f"قيمة model_settings: {bot_settings.get('model_settings')}")
        # تحضير مطلب للبوت
        prompt = _prepare_prompt(user_message, relevant_knowledge, conversation_history, bot_settings)

        # الحصول على رد من AI API
        response = await ai_service.get_response(prompt, bot_settings.get('model_settings', {}))

        # حفظ المحادثة في قاعدة البيانات
        conversation_id = await chat_manager.save_conversation(
            user_id, session_id, user_message, response,
            [k['id'] for k in relevant_knowledge] if relevant_knowledge else None
        )

        return jsonify({
            'response': response,
            'session_id': session_id,
            'conversation_id': conversation_id
        })

    except Exception as e:
        current_app.logger.error(f"خطأ في المحادثة: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء معالجة طلبك'}), 500


@chatbot_bp.route('/feedback', methods=['POST'])
async def submit_feedback():
    """تلقي تقييم المستخدم للمحادثة"""
    try:
        data = await request.get_json()

        # التحقق من البيانات المدخلة
        if not data:
            return jsonify({'error': 'البيانات غير صالحة'}), 400

        conversation_id = data.get('conversation_id')
        if not conversation_id:
            return jsonify({'error': 'معرف المحادثة مطلوب'}), 400

        rating = data.get('rating')
        if not rating or not isinstance(rating, (int, str)):
            return jsonify({'error': 'التقييم مطلوب'}), 400

        feedback = data.get('feedback', '')

        await chat_manager.save_feedback(conversation_id, rating, feedback)

        return jsonify({'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"خطأ في حفظ التقييم: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء حفظ التقييم'}), 500




@chatbot_bp.route('/analyze-conversation', methods=['GET'])
async def analyze_conversation():
    """تحليل بيانات المحادثات للحصول على إحصائيات"""
    try:
        # الحصول على المعلمات
        days = int(request.args.get('days', 7))

        # تنظيف المدخلات
        if days <= 0 or days > 90:
            days = 7  # قيمة افتراضية آمنة

        async with current_app.db_pool.acquire() as conn:
            # إحصائيات حول التقييمات
            ratings = await conn.fetch(
                """
                SELECT rating, COUNT(*) as count
                FROM conversations
                WHERE created_at >= CURRENT_DATE - $1::interval
                AND rating IS NOT NULL
                GROUP BY rating
                ORDER BY rating
                """,
                f"{days} days"
            )

            # المواضيع الأكثر شيوعًا (باستخدام الكلمات المفتاحية من الأسئلة)
            # هذا مثال بسيط، يمكن تحسينه باستخدام تحليل نصي متقدم
            topics = await conn.fetch(
                """
                SELECT CASE
                    WHEN user_message ILIKE '%سعر%' OR user_message ILIKE '%تكلفة%' THEN 'الأسعار'
                    WHEN user_message ILIKE '%خدمة%' OR user_message ILIKE '%دعم%' THEN 'الدعم الفني'
                    WHEN user_message ILIKE '%توصيل%' OR user_message ILIKE '%شحن%' THEN 'التوصيل'
                    WHEN user_message ILIKE '%حساب%' OR user_message ILIKE '%تسجيل%' THEN 'الحسابات'
                    ELSE 'أخرى'
                END as topic,
                COUNT(*) as count
                FROM conversations
                WHERE created_at >= CURRENT_DATE - $1::interval
                GROUP BY topic
                ORDER BY count DESC
                """,
                f"{days} days"
            )

            # متوسط وقت الاستجابة (إذا كانت البيانات متوفرة)
            avg_response_time = await conn.fetchval(
                """
                SELECT AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))
                FROM conversations
                WHERE created_at >= CURRENT_DATE - $1::interval
                """,
                f"{days} days"
            )

            return jsonify({
                'ratings': [dict(row) for row in ratings],
                'topics': [dict(row) for row in topics],
                'avg_response_time': avg_response_time or 0,
                'period_days': days
            })

    except Exception as e:
        current_app.logger.error(f"خطأ في تحليل المحادثات: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء تحليل بيانات المحادثات'}), 500


async def _get_bot_settings():
    async with current_app.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM bot_settings ORDER BY id DESC LIMIT 1'
        )
        if not row:
            # إعدادات افتراضية إذا لم يتم العثور على إعدادات
            return {
                'name': 'دعم عملاء اكسادوا',
                'prompt_template': """أنت مساعد دعم العملاء لشركة اكسادوا. اتبع هذه الإرشادات:

                    1. قدم إجابات مباشرة ومختصرة.
                    2. استخدم لغة مهذبة واحترافية.
                    3. إذا لم تكن متأكدًا، اقترح التواصل مع فريق الدعم.
                    4. لا تختلق معلومات غير موجودة في السياق.
                    5. قدم خطوات واضحة ومرقمة للإجراءات والحلول.
                    6. استخدم صيغة المتحدث الرسمي باسم الشركة.

                    {conversation_history}
                    {context}""",
                'welcome_message': 'مرحباً بك! كيف يمكنني مساعدتك اليوم؟',
                'fallback_message': 'آسف، لا يمكنني الإجابة على هذا السؤال. هل يمكنني مساعدتك بشيء آخر؟',
                'model_settings': {'temperature': 0.1, 'max_tokens': 500}
            }

        settings = dict(row)
        # إذا كان field model_settings نص، فكّك الـ JSON
        if isinstance(settings.get('model_settings'), str):
            try:
                settings['model_settings'] = json.loads(settings['model_settings'])
            except json.JSONDecodeError:
                # في حال كان النص ليس JSON صالح
                settings['model_settings'] = {}

        return settings

def _prepare_prompt(user_message, knowledge_items, conversation_history, settings):
    """تحضير المطلب للبوت باستخدام قاعدة المعرفة وسجل المحادثات"""
    # إعداد سياق من قاعدة المعرفة
    context = ""
    if knowledge_items:
        context = "استخدم المعلومات التالية للإجابة على سؤال المستخدم. إذا لم تتمكن من العثور على إجابة من هذه المعلومات، فأخبر المستخدم أنك لا تملك المعلومات الكافية:\n\n"
        for item in knowledge_items:
            context += f"- {item['content']}\n\n"

    # إعداد سجل المحادثات السابقة
    conv_history_text = ""
    if conversation_history:
        conv_history_text = "المحادثات السابقة:\n"
        for conv in reversed(conversation_history):  # عرض من الأقدم للأحدث
            conv_history_text += f"المستخدم: {conv['user_message']}\n"
            conv_history_text += f"البوت: {conv['bot_response']}\n\n"

    # استبدال العلامات في قالب المطلب
    prompt = settings['prompt_template'].format(
        context=context,
        conversation_history=conv_history_text
    )
    prompt += f"\n\nسؤال المستخدم: {user_message}\n\nالإجابة:"

    return prompt


def _sanitize_input(text):
    """تنظيف النص المدخل لمنع هجمات الحقن"""
    if not text:
        return text

    # إزالة أي محاولات حقن SQL
    text = re.sub(r'[\'"\\;]', '', text)
    return text