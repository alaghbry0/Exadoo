# chatbot.py
from quart import Blueprint, request, current_app, jsonify
import asyncio
import uuid
import re
import time
import json

chatbot_bp = Blueprint('chatbot', __name__)

HISTORY_LIMIT = 10
CACHE_TTL = 300  # ثواني
MAX_KNOWLEDGE_ITEMS = 5

@chatbot_bp.route('/chat/stream', methods=['POST', 'OPTIONS'])
async def chat_stream():
    """Stream response from AI service to the client using SSE"""
    if request.method == 'OPTIONS':
        # عودة مع رؤوس CORS فقط
        return '', 200, _sse_headers()
    try:
        
        data = await request.get_json()
        if not data:
            return _error_event('Invalid data'), 400, _sse_headers()

        # Extract & sanitize
        user_message = data.get('message')
        session_id   = _sanitize_input(data.get('session_id', str(uuid.uuid4())))
        user_id      = _sanitize_input(data.get('user_id', 'anonymous'))
        search_limit = data.get('search_limit', MAX_KNOWLEDGE_ITEMS)
        debug_mode   = data.get('debug', False)

        if not user_message or not isinstance(user_message, str):
            return _error_event('Valid message is required'), 400, _sse_headers()
        user_message = _sanitize_input(user_message)

        # injected services
        ai_service   = current_app.ai_service
        chat_manager = current_app.chat_manager
        kb           = current_app.kb

        async def stream_response():
            start_time = time.time()
            # fetch settings within app context
            bot_settings = await _get_bot_settings()

            # parallel tasks: search & history
            knowledge_task    = asyncio.create_task(
                kb.search(user_message, limit=search_limit, debug=debug_mode)
            )
            conversation_task = asyncio.create_task(
                chat_manager.get_conversation_with_cache(session_id, limit=HISTORY_LIMIT)
            )
            relevant_knowledge, conversation_data = await asyncio.gather(
                knowledge_task, conversation_task
            )

            history     = conversation_data.get('history', [])
            kv_cache_id = conversation_data.get('kv_cache_id')

            # debug info
            if debug_mode:
                info = [
                    {'id': k['id'], 'title': k['title'], 'score': k.get('score', 0)}
                    for k in relevant_knowledge[:3]
                ]
                yield f"event: debug\ndata: {json.dumps({'search_results': info})}\n\n"

            # fallback
            if not relevant_knowledge:
                fallback = bot_settings.get('fallback_message', 'Sorry, no information available.')
                yield f"event: message\ndata: {json.dumps({'message': fallback})}\n\n"
                yield f"event: end\ndata: {json.dumps({'session_id': session_id})}\n\n"
                return

            # build messages
            system_prompt = bot_settings.get('system_instructions', '')
            messages = [{'role': 'system', 'content': system_prompt}]

            context = "\n\n".join(
                f"### Source {i+1}: {item['title']}\n{item['snippet'][:500]}..."
                for i, item in enumerate(relevant_knowledge)
            )
            messages.append({
                'role': 'system',
                'content': f"Use the following information:\n---\n{context}\n---"
            })
            messages.extend(history)
            messages.append({'role': 'user', 'content': user_message})

            tools = _define_tools()
            full_resp = ''

            # stream AI chunks
            async for chunk in ai_service.stream_response(
                messages=messages,
                settings={
                    'temperature':      bot_settings.get('temperature', 0.1),
                    'max_tokens':       bot_settings.get('max_tokens', 500),
                    'session_id':       session_id,
                    'kv_cache_id':      kv_cache_id,
                    'tools':            tools,
                    'reasoning_enabled':bot_settings.get('reasoning_enabled', False),
                    'reasoning_steps':  bot_settings.get('reasoning_steps', 3)
                }
            ):
                if chunk.get('content'):
                    full_resp += chunk['content']
                    yield f"event: chunk\ndata: {json.dumps(chunk)}\n\n"
                if chunk.get('tool_calls'):
                    yield f"event: tool_call\ndata: {json.dumps({'tool_calls': chunk['tool_calls']})}\n\n"
                    for call in chunk['tool_calls']:
                        name = call['function']['name']
                        args = json.loads(call['function']['arguments'])
                        result = await _execute_tool_call(name, args, session_id)
                        yield f"event: tool_result\ndata: {json.dumps({'tool_result': result, 'tool_call_id': call['id']})}\n\n"

            # save conversation
            conv_id = await chat_manager.save_conversation(
                user_id, session_id, user_message, full_resp,
                [k['id'] for k in relevant_knowledge], []
            )

            # end event
            end_payload = {'session_id': session_id, 'conversation_id': conv_id}
            if debug_mode:
                end_payload['debug'] = {'execution_time_ms': round((time.time()-start_time)*1000,2)}
            yield f"event: end\ndata: {json.dumps(end_payload)}\n\n"

        return stream_response(), 200, _sse_headers()

    except Exception as e:
        current_app.logger.error(f"Error in chat_stream: {e}", exc_info=True)
        return _error_event(str(e)), 500, _sse_headers()


# Helpers

def _sse_headers():
    return {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    }


def _error_event(details: str) -> str:
    return f"event: error\ndata: {json.dumps({'error':'An error occurred','details':details})}\n\n"


def _sanitize_input(text):
    """تنظيف المدخلات مع الحفاظ على الروابط"""
    if not text:
        return ''

    # استخراج الروابط بكفاءة
    urls = URL_PATTERN.findall(text)
    replacements = []

    # استبدال الروابط بعلامات مؤقتة
    for i, url in enumerate(urls):
        placeholder = f"__URL_{i}__"
        text = text.replace(url, placeholder)
        replacements.append((placeholder, url))

    # تنظيف النص
    sanitized = SQL_INJECTION_PATTERN.sub("", text)

    # إعادة الروابط الأصلية
    for ph, url in replacements:
        sanitized = sanitized.replace(ph, url)

    return sanitized.strip()[:2000] if sanitized else ''




def _define_tools() -> list:
    return [
        {
            'type': 'function',
            'function': {
                'name': 'search_knowledge_base',
                'description': 'Search for specific info in KB',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string', 'description': 'Search query'}
                    },
                    'required': ['query']
                }
            }
        },
        {
            'type': 'function',
            'function': {
                'name': 'escalate_to_human',
                'description': 'Escalate to human agent',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'reason': {'type': 'string', 'description': 'Reason for escalation'},
                        'priority': {
                            'type': 'string',
                            'enum': ['low','medium','high','urgent'],
                            'description': 'Escalation priority'
                        }
                    },
                    'required': ['reason']
                }
            }
        }
    ]


async def _execute_tool_call(function_name: str, args: dict, session_id: str) -> dict:
    try:
        if function_name == 'search_knowledge_base':
            query = args.get('query','')
            if not query:
                return {'error':'No query provided'}
            results = await current_app.kb.search(query, limit=3)
            return {'results':[{'title':i['title'],'content':i.get('content','')[:300]+'...' if len(i.get('content',''))>300 else i.get('content','')} for i in results],'count':len(results)}
        elif function_name == 'escalate_to_human':
            reason = args.get('reason','Unspecified')
            priority = args.get('priority','medium')
            current_app.logger.info(f"ESCALATION {session_id}: {reason} ({priority})")
            async with current_app.db_pool.acquire() as conn:
                await conn.execute("INSERT INTO escalations(session_id,reason,priority,created_at) VALUES($1,$2,$3,NOW())", session_id, reason, priority)
            return {'status':'escalated','ticket_id':f"ESC-{int(time.time())}-{session_id[-5:]}",'estimated_wait':'5-10 minutes' if priority in ['high','urgent'] else 'up to 24 hours'}
        else:
            return {'error':f'Unknown function: {function_name}'}
    except Exception as e:
        current_app.logger.error(f"Tool error ({function_name}): {e}")
        return {'error':str(e)}

async def _get_bot_settings() -> dict:
    """Retrieve bot settings with caching, safe outside app context"""
    default_settings = {
        'name': 'دعم عملاء اكسادوا',
        'system_instructions': """أنت مساعد دعم العملاء لشركة اكسادوا. اتبع هذه الإرشادات:
    1. استخدم المعلومات المقدمة فقط مع الإشارة للمصادر
    2. قدم إجابات مختصرة ومركزة
    3. استخدم تنسيق Markdown للتنظيم
    4. عند عدم التأكد، اطلب توضيحاً أو صعد إلى الدعم البشري""",
        'welcome_message': 'مرحباً بك! كيف يمكنني مساعدتك اليوم؟',
        'faq_questions': [
            "كيف يمكنني تتبع حالة طلبي؟",
            "ما هي طرق الدفع المتاحة؟",
            "كيفية إرجاع منتج؟",
            "ما هي مدة التوصيل المتوقعة؟"
        ],
        'fallback_message': 'آسف، لا يمكنني الإجابة على هذا السؤال. هل يمكنني مساعدتك بشيء آخر؟',
        'temperature': 0.1,
        'max_tokens': 500,
        'reasoning_enabled': True,
        'reasoning_steps': 3
    }
    try:
        app = current_app
        # use cache
        if hasattr(app, 'bot_settings_cache'):
            cached, ts = app.bot_settings_cache
            if time.time()-ts < CACHE_TTL:
                return cached
        # fetch from DB
        async with app.db_pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM bot_settings ORDER BY id DESC LIMIT 1')
        if not row:
            app.bot_settings_cache = (default_settings, time.time())
            return default_settings
        settings = dict(row)
        # parse fields
        for f in ['faq_questions','system_instructions']:
            if isinstance(settings.get(f), str):
                try: settings[f] = json.loads(settings[f])
                except: settings[f] = default_settings[f]
        # defaults
        for k,v in default_settings.items(): settings.setdefault(k,v)
        settings['temperature']=float(settings['temperature'])
        settings['max_tokens']=int(settings['max_tokens'])
        settings['reasoning_enabled']=bool(settings['reasoning_enabled'])
        settings['reasoning_steps']=int(settings['reasoning_steps'])
        # cache
        app.bot_settings_cache = (settings, time.time())
        return settings
    except RuntimeError:
        # outside app context
        return default_settings
    except Exception as e:
        current_app.logger.error(f"Error getting bot settings: {e}")
        return default_settings

URL_PATTERN = re.compile(r'https?://\S+')


def _prepare_prompt(user_message, knowledge_items, conversation_history, settings):
    """تحضير المطلب للبوت مع تحسينات الأداء والدقة والهيكلة الجديدة"""
    # ترتيب العناصر حسب الأهمية مع مراعاة الروابط
    sorted_items = sorted(
        knowledge_items[:MAX_KNOWLEDGE_ITEMS],
        key=lambda x: (
            x.get('score', 0),
            0.5 if any(word in user_message.lower() for word in ['رابط', 'صفحة', 'موقع'])
                   and 'http' in x.get('content', '') else 0
        ),
        reverse=True
    )

    # بناء سياق المعلومات مع تحسينات التنسيق
    context_parts = []
    total_length = 0

    for i, item in enumerate(sorted_items):
        title = item.get('title', '')
        content = item.get('content', '')
        url_matches = []  # تهيئة أولية

        # تحسين استخراج المحتوى المهم مع الحفاظ على الروابط
        if len(content) > 500:
            relevant_sections = []
            url_matches = URL_PATTERN.findall(content)  # استخدام الثابت المحدد مسبقاً

            # إضافة الأقسام التي تحتوي على روابط أولاً
            for url in url_matches:
                url_pos = content.find(url)
                if url_pos != -1:
                    start = max(0, url_pos - 100)
                    end = min(len(content), url_pos + len(url) + 50)
                    relevant_sections.append(content[start:end])

            # إضافة بداية المحتوى إذا لم يحتوي على روابط
            if not relevant_sections:
                relevant_sections.append(content[:250])

            content = " ... ".join(relevant_sections)[:500]

        # بناء عنصر السياق مع التنسيق المحسن
        item_text = f"### مصدر {i + 1}: {title}\n{content}"
        if url_matches:
            item_text += f"\n\nالروابط ذات الصلة:\n" + "\n".join(f"- {url}" for url in url_matches[:3])

        if total_length + len(item_text) > MAX_CONTEXT_LENGTH:
            remaining_length = MAX_CONTEXT_LENGTH - total_length - 10
            item_text = item_text[:remaining_length] + "... [محتوى مقتطع]"
            context_parts.append(item_text)
            break

        context_parts.append(item_text)
        total_length += len(item_text)

    context = "\n\n".join(context_parts)

    # بناء سجل المحادثة مع تحسينات التنسيق
    conv_history = []
    for conv in conversation_history[-HISTORY_LIMIT:]:
        entry = (
            f"**المستخدم**: {conv['user_message'][:200]}\n"
            f"**البوت**: {conv['bot_response'][:300]}"
        )
        conv_history.append(entry)

    conv_history_text = "## سجل المحادثة السابقة\n" + "\n\n".join(conv_history) if conv_history else ""

    # الحصول على القوالب من الإعدادات مع قيم افتراضية
    system_instructions = settings.get('system_instructions', 'الرجاء اتباع سياسة الدعم الأساسية.')

    default_template = """{system_instructions}

--- السياق ---
{context}

--- المحادثة السابقة ---
{conversation_history}

--- السؤال ---
{user_message}

--- الإجابة ---"""

    response_template = settings.get('response_template', default_template)

    # بناء المطلب مع معالجة الأخطاء
    try:
        prompt = response_template.format(
            system_instructions=system_instructions,
            context=context,
            conversation_history=conv_history_text,
            user_message=user_message
        )
        logging.info(f"{prompt}")
    except KeyError as e:
        logging.error(f"مفتاح مفقود في القالب: {str(e)}")
        prompt = f"{system_instructions}\n\n{user_message}"

    # التحقق من الطول الإجمالي
    if len(prompt) > MAX_CONTEXT_LENGTH * 2:
        current_app.logger.warning(f"طول المطلب تجاوز الحد ({len(prompt)} حرف)")
        prompt = prompt[:MAX_CONTEXT_LENGTH * 2] + "... [محتوى مقتطع]"

    logging.info(f"تم إنشاء مطلب بطول {len(prompt)} حرف")
    return prompt


SQL_INJECTION_PATTERN = re.compile(r"""
    [;'"\\<>]|
    \b(ALTER|CREATE|DELETE|DROP|EXEC|INSERT|MERGE|SELECT|UPDATE|UNION)\b
""", re.IGNORECASE | re.VERBOSE)


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


@chatbot_bp.route('/welcome', methods=['GET'])
async def get_welcome_message():
    """إرسال الرسالة الترحيبية والأسئلة الشائعة عند فتح الدردشة"""
    try:
        start_time = time.time()
        # جلب أحدث إعدادات البوت مباشرة من قاعدة البيانات
        async with current_app.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  welcome_message,
                  faq_questions
                FROM bot_settings
                ORDER BY created_at DESC
                LIMIT 1
                """
            )

        if row:
            welcome_message = row['welcome_message']
            faq_questions = row['faq_questions']
        else:
            # قيم افتراضية في حال عدم وجود سجل
            welcome_message = 'مرحباً بك! كيف يمكنني مساعدتك اليوم؟'
            faq_questions = [
                'كيف يمكنني تتبع حالة طلبي؟',
                'ما هي طرق الدفع المتاحة؟',
                'كيفية إرجاع منتج؟',
                'ما هي مدة التوصيل المتوقعة؟'
            ]

        # تحضير الاستجابة
        response = {
            'welcome_message': welcome_message,
            'faq_questions': faq_questions
        }

        # قياس الأداء (عند تفعيل الـ debug)
        if current_app.debug:
            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            response['debug'] = {'execution_time_ms': elapsed_ms}

        return jsonify(response)

    except Exception as e:
        current_app.logger.error(f"خطأ في استرجاع الترحيب: {e}")
        return jsonify({'error': 'حدث خطأ أثناء جلب البيانات الترحيبية'}), 500



@chatbot_bp.route('/chat', methods=['POST'])
async def chat():
    """Receive user message and send response"""
    start_time = time.time()

    try:
        data = await request.get_json()

        # Validate input data
        if not data:
            return jsonify({'error': 'Invalid data'}), 400

        user_message = data.get('message')
        if not user_message or not isinstance(user_message, str):
            return jsonify({'error': 'Valid message is required'}), 400

        # Sanitize user message to prevent injection attacks
        user_message = _sanitize_input(user_message)

        session_id = data.get('session_id', str(uuid.uuid4()))
        user_id = data.get('user_id', 'anonymous')

        # Requested number of search results (optional)
        search_limit = data.get('search_limit', MAX_KNOWLEDGE_ITEMS)

        # Enable debug mode (optional)
        debug_mode = data.get('debug', False)

        # Sanitize user and session IDs
        session_id = _sanitize_input(session_id)
        user_id = _sanitize_input(user_id)

        # Parallel tasks: search knowledge base, get conversation history, and get bot settings
        knowledge_task = asyncio.create_task(
            knowledge_base.search(user_message, limit=search_limit, debug=debug_mode)
        )

        # Use new method to get both history and KV cache
        conversation_task = asyncio.create_task(
            chat_manager.get_conversation_with_cache(session_id, limit=HISTORY_LIMIT)
        )

        settings_task = asyncio.create_task(_get_bot_settings())

        # Gather all task results
        relevant_knowledge, conversation_data, bot_settings = await asyncio.gather(
            knowledge_task, conversation_task, settings_task
        )

        # Extract conversation history and KV cache ID
        conversation_history = conversation_data.get("history", [])
        kv_cache_id = conversation_data.get("kv_cache_id")

        # If no relevant knowledge, use fallback message
        if not relevant_knowledge:
            response_data = {
                'response': bot_settings.get('fallback_message', 'Sorry, not enough information available.'),
                'session_id': session_id
            }

            if debug_mode:
                response_data['debug'] = {
                    'execution_time_ms': round((time.time() - start_time) * 1000, 2),
                    'search_results': []
                }

            return jsonify(response_data)

        # Build message list for the API
        system_prompt = bot_settings.get('system_instructions', 'You are a customer support assistant...')

        # Prepare messages list
        messages_for_api = [{"role": "system", "content": system_prompt}]

        # Add knowledge context as a system message
        if relevant_knowledge:
            context_str = "\n\n".join([
                f"### Source {i + 1}: {item['title']}\n{item['snippet'][:500]}..."
                for i, item in enumerate(relevant_knowledge)
            ])
            messages_for_api.append({
                "role": "system",
                "content": f"Use the following information to respond:\n---\n{context_str}\n---"
            })

        # Add conversation history
        messages_for_api.extend(conversation_history)

        # Add current user message
        messages_for_api.append({"role": "user", "content": user_message})

        # Define tools for function calling
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_knowledge_base",
                    "description": "Search for more specific information in the knowledge base",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query to find more specific information"
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "escalate_to_human",
                    "description": "Escalate the conversation to a human support agent",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "The reason for escalation"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "urgent"],
                                "description": "The priority level for the escalation"
                            }
                        },
                        "required": ["reason"]
                    }
                }
            }
        ]

        # Call AI service with all prepared data
        ai_response = await ai_service.get_response(
            messages=messages_for_api,
            settings={
                'temperature': bot_settings.get('temperature', 0.1),
                'max_tokens': bot_settings.get('max_tokens', 500),
                'session_id': session_id,
                'kv_cache_id': kv_cache_id,
                'tools': tools,
                'reasoning_enabled': bot_settings.get('reasoning_enabled', False),
                'reasoning_steps': bot_settings.get('reasoning_steps', 3)
            }
        )

        # Check if tool calls are present
        executed_tools = []

        if ai_response.get('tool_calls'):
            # Add assistant's response to messages
            messages_for_api.append({
                "role": "assistant",
                "content": ai_response.get('content', ''),
                "tool_calls": ai_response.get('tool_calls')
            })

            # Execute each tool call
            for tool_call in ai_response.get('tool_calls', []):
                function_name = tool_call.get('function', {}).get('name')
                function_args = json.loads(tool_call.get('function', {}).get('arguments', '{}'))
                tool_call_id = tool_call.get('id')

                tool_result = await _execute_tool_call(function_name, function_args, session_id)

                # Add tool result to messages
                messages_for_api.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(tool_result)
                })

                executed_tools.append({
                    "name": function_name,
                    "args": function_args,
                    "result": tool_result
                })

            # Get final response after tool execution
            final_response = await ai_service.get_response(
                messages=messages_for_api,
                settings={
                    'temperature': bot_settings.get('temperature', 0.1),
                    'max_tokens': bot_settings.get('max_tokens', 500),
                    'session_id': session_id,
                    'kv_cache_id': kv_cache_id,
                    'reasoning_enabled': bot_settings.get('reasoning_enabled', False),
                    'reasoning_steps': bot_settings.get('reasoning_steps', 3)
                }
            )

            response_content = final_response.get('content', '')
        else:
            # No tool calls, use the direct response
            response_content = ai_response.get('content', '')

        # Save conversation to database
        conversation_id = await chat_manager.save_conversation(
            user_id, session_id, user_message, response_content,
            [k['id'] for k in relevant_knowledge] if relevant_knowledge else None,
            executed_tools if executed_tools else None
        )

        # Prepare response data
        response_data = {
            'response': response_content,
            'session_id': session_id,
            'conversation_id': conversation_id
        }

        # Add debug info if requested
        if debug_mode:
            end_time = time.time()
            knowledge_info = [{
                'id': k['id'],
                'title': k['title'],
                'score': k.get('score', 0),
                'distance': k.get('distance', None)
            } for k in relevant_knowledge[:5]]

            response_data['debug'] = {
                'execution_time_ms': round((end_time - start_time) * 1000, 2),
                'search_results': knowledge_info,
                'message_count': len(messages_for_api),
                'tool_calls': executed_tools if executed_tools else None
            }

        return jsonify(response_data)

    except Exception as e:
        current_app.logger.error(f"Error in chat: {str(e)}", exc_info=True)
        return jsonify({
            'error': 'An error occurred while processing your request',
            'details': str(e) if current_app.debug else None
        }), 500
