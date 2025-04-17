from quart import Blueprint, request, jsonify, current_app
from chatbot.knowledge_base import KnowledgeBase
from chatbot.ai_service import DeepSeekService
from chatbot.chat_manager import ChatManager
import asyncio
import uuid

chatbot_bp = Blueprint('chatbot', __name__)
knowledge_base = KnowledgeBase()
ai_service = DeepSeekService()
chat_manager = ChatManager()


@chatbot_bp.route('/chat', methods=['POST'])
async def chat():
    """تلقي رسالة المستخدم وإرسال الرد"""
    try:
        data = await request.get_json()
        user_message = data.get('message')
        session_id = data.get('session_id', str(uuid.uuid4()))
        user_id = data.get('user_id', 'anonymous')

        # البحث في قاعدة المعرفة
        relevant_knowledge = await knowledge_base.search(user_message)

        # الحصول على إعدادات البوت من قاعدة البيانات
        bot_settings = await _get_bot_settings()

        # تحضير مطلب للبوت
        prompt = _prepare_prompt(user_message, relevant_knowledge, bot_settings)

        # الحصول على رد من DeepSeek API
        response = await ai_service.get_response(prompt)

        # حفظ المحادثة في قاعدة البيانات
        conversation_id = await chat_manager.save_conversation(
            user_id, session_id, user_message, response,
            [k['id'] for k in relevant_knowledge]
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
        conversation_id = data.get('conversation_id')
        rating = data.get('rating')
        feedback = data.get('feedback', '')

        await chat_manager.save_feedback(conversation_id, rating, feedback)

        return jsonify({'status': 'success'})

    except Exception as e:
        current_app.logger.error(f"خطأ في حفظ التقييم: {str(e)}")
        return jsonify({'error': 'حدث خطأ أثناء حفظ التقييم'}), 500


async def _get_bot_settings():
    """الحصول على إعدادات البوت من قاعدة البيانات"""
    async with current_app.db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM bot_settings ORDER BY id DESC LIMIT 1')
        if row:
            return dict(row)
        else:
            # إعدادات افتراضية إذا لم يتم العثور على إعدادات
            return {
                'name': 'دعم عملاء اكسادوا',
                'prompt_template':

                    """
                       أنت مساعد دعم العملاء لشركة اكسادوا. اتبع هذه الإرشادات:
    
                       1. قدم إجابات مباشرة ومختصرة.
                       2. استخدم لغة مهذبة واحترافية.
                       3. إذا لم تكن متأكدًا، اقترح التواصل مع فريق الدعم.
                       4. لا تختلق معلومات غير موجودة في السياق.
                       5. قدم خطوات واضحة ومرقمة للإجراءات والحلول.
                       6. استخدم صيغة المتحدث الرسمي باسم الشركة.
                       {context}""",

                'welcome_message': 'مرحباً بك! كيف يمكنني مساعدتك اليوم؟',
                'fallback_message': 'آسف، لا يمكنني الإجابة على هذا السؤال. هل يمكنني مساعدتك بشيء آخر؟',
                'model_settings': {'temperature': 0.1, 'max_tokens': 30}
            }


def _prepare_prompt(user_message, knowledge_items, settings):
    """تحضير المطلب للبوت باستخدام قاعدة المعرفة"""
    context = ""

    if knowledge_items:
        context = "استخدم المعلومات التالية فقط للإجابة على سؤال المستخدم. إذا لم تتمكن من العثور على إجابة من هذه المعلومات، فأخبر المستخدم أنك لا تملك المعلومات الكافية وطلب توضيحًا:\n\n"
        for item in knowledge_items:
            context += f"- {item['content']}\n\n"

    # استبدال العلامات في قالب المطلب
    prompt = settings['prompt_template'].format(context=context)
    prompt += f"\n\nسؤال المستخدم: {user_message}\n\nالإجابة:"

    return prompt