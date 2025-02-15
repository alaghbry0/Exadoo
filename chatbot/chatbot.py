from quart import Blueprint, request, jsonify
import google.generativeai as genai
import os

chatbot_bp = Blueprint('chatbot_bp', __name__)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-pro')

conversation_history = {}

# تحميل System Prompt من ملف خارجي
with open("system_prompt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

# تحميل قاعدة المعرفة من ملف خارجي (لتبسيط مؤقت)
with open("knowledge_base.txt", "r", encoding="utf-8") as f:
    KNOWLEDGE_BASE = f.read()

# دمج قاعدة المعرفة في System Prompt
SYSTEM_PROMPT_WITH_KNOWLEDGE = f"""
{SYSTEM_PROMPT}

**قاعدة المعرفة:**
{KNOWLEDGE_BASE}
"""

@chatbot_bp.route("/chat", methods=["POST"])
async def chat():
    data = await request.get_json()
    user_id = data.get("user_id")
    message = data.get("message")

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    if message == "/start":
        reply = "مرحباً بك في روبوت الدعم الخاص بأكسادوا! أنا هنا لمساعدتك في أي أسئلة لديك حول أكسادوا ومنتجاتها وخدماتها. كيف يمكنني مساعدتك اليوم؟"
        conversation_history[user_id] = []
        conversation_history[user_id].append({"role": "user", "parts": [{"text": SYSTEM_PROMPT_WITH_KNOWLEDGE}]}) # استخدام SYSTEM_PROMPT_WITH_KNOWLEDGE
        return jsonify({"reply": reply})

    try:
        # إضافة رسالة المستخدم إلى سجل المحادثة
        conversation_history[user_id].append({"role": "user", "parts": [{"text": message}]})

        # إرسال سجل المحادثة بالكامل إلى Gemini API
        response = model.generate_content(
            contents=conversation_history[user_id]
        )

        reply = response.text
        # إضافة رد الروبوت إلى سجل المحادثة
        conversation_history[user_id].append({"role": "model", "parts": [{"text": reply}]})

        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500