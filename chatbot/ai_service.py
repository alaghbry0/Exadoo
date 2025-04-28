# ai_service.py - Modified version
import aiohttp
import json
import time
import asyncio
import logging
from typing import Dict, Any, List, Optional


class DeepSeekService:
    """
    Service for interacting with DeepSeek AI via streaming and prefix completions.
    Requires initialization with Quart app to inject db_pool and logger.
    """
    def __init__(self, app=None):
        # DB pool and logger will be set in init_app;
        # provide a fallback logger until then.
        self.db_pool = None
        self.logger = logging.getLogger(__name__)

        # DeepSeek endpoints and defaults
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        self.api_url_beta = "https://api.deepseek.com/beta/chat/completions"
        self.default_model = "deepseek-chat"

        # API key caching mechanism
        self._api_key_cache = {"api_key": None, "timestamp": 0.0}
        self.API_KEY_CACHE_TTL = 300  # 5 دقائق

        if app:
            self.init_app(app)

    def init_app(self, app) -> None:
        """Initialize service with application dependencies."""
        self.db_pool = app.db_pool
        self.logger = app.logger or self.logger

    async def get_deepseek_api_key(self) -> Optional[str]:
        """Retrieve API key from database, with simple TTL caching."""
        now = time.time()
        # Reload key if TTL expired or not yet fetched
        if (self._api_key_cache["api_key"] is None or
                now - self._api_key_cache["timestamp"] > self.API_KEY_CACHE_TTL):
            if not self.db_pool:
                self.logger.error("Database pool not initialized")
                return None

            try:
                async with self.db_pool.acquire() as conn:
                    record = await conn.fetchrow(
                        "SELECT api_key FROM wallet ORDER BY id DESC LIMIT 1"
                    )
                if not record or not record.get('api_key'):
                    self.logger.error("No API key found in database")
                    return None

                # Update cache
                self._api_key_cache['api_key'] = record['api_key']
                self._api_key_cache['timestamp'] = now
                self.logger.info("API key updated from database")

            except Exception as e:
                self.logger.error(f"Error retrieving API key: {e}")
                return None

        return self._api_key_cache['api_key']

    async def stream_response(self, messages: List[Dict[str, str]], settings: dict = None):
        """
        Stream a response from DeepSeek API with Server-Sent Events (SSE)
        """
        # 1. Get API key from database
        api_key = await self.get_deepseek_api_key()
        if not api_key:
            self.logger.error("DeepSeek API key not available")
            yield {"role": "assistant", "content": "عذرًا، لا يمكن الاتصال بخدمة الذكاء الاصطناعي حاليًا."}
            return

        # 2. Ensure valid settings
        if not isinstance(settings, dict):
            settings = {}

        # Update model parameters
        temperature = float(settings.get("temperature", 0.1))
        max_tokens = int(settings.get("max_tokens", 500))
        model = settings.get("model", self.default_model)

        # 3. Check for excessive length in messages
        total_length = sum(len(msg.get("content", "")) for msg in messages)
        if total_length > 32000:  # Typical context window limit
            self.logger.info("Total messages too large, trimming")
            # Trim user messages while preserving system messages
            system_messages = [m for m in messages if m.get("role") == "system"]
            user_assistant_messages = [m for m in messages if m.get("role") != "system"]

            # Keep most recent messages
            preserved_messages = user_assistant_messages[-10:]  # Keep last 10 exchanges
            messages = system_messages + preserved_messages

        # 4. Prepare headers and payload
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True  # Important! Enable streaming
        }

        # Add function calling if provided
        if settings.get("tools"):
            payload["tools"] = settings.get("tools")

        if settings.get("tool_choice"):
            payload["tool_choice"] = settings.get("tool_choice")

        # Add reasoning_step if enabled
        if settings.get("reasoning_enabled"):
            payload["reasoning_step"] = settings.get("reasoning_steps", 3)

        # Add KV cache if available
        if settings.get("kv_cache_id"):
            payload["kv_cache_id"] = settings.get("kv_cache_id")

        # 5. Setup connection timeout
        api_timeout = 120  # Longer timeout for streaming responses
        start = time.time()

        try:
            # 6. Setup session and streaming
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        self.api_url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=api_timeout)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.logger.error(f"DeepSeek API error: {resp.status} – {error_text}")
                        yield {"role": "assistant", "content": "عذرًا، حدث خطأ في الاتصال بالخدمة."}
                        return

                    # Process streaming response
                    current_chunk = {"role": "assistant", "content": ""}
                    tool_calls_buffer = []
                    tool_call_index = 0

                    # Variable to track if we're inside a tool call
                    in_tool_call = False
                    current_tool_call = None

                    # Read the response stream
                    async for line in resp.content:
                        line = line.decode('utf-8').strip()

                        # Skip empty lines
                        if not line:
                            continue

                        # Skip comments
                        if line.startswith(':'):
                            continue

                        # Check for data line
                        if line.startswith('data: '):
                            data_str = line[6:]  # Remove 'data: ' prefix

                            # Check for stream end
                            if data_str == '[DONE]':
                                break

                            try:
                                data = json.loads(data_str)

                                # Extract delta and type
                                if 'choices' in data and len(data['choices']) > 0:
                                    choice = data['choices'][0]
                                    delta = choice.get('delta', {})

                                    # Handle normal content
                                    if 'content' in delta and delta['content']:
                                        current_chunk["content"] += delta['content']
                                        # Send content chunk
                                        yield {"content": delta['content']}

                                    # Handle tool calls
                                    if 'tool_calls' in delta:
                                        for tool_call_delta in delta['tool_calls']:
                                            tool_call_id = tool_call_delta.get('id')

                                            # Check if this is a new tool call
                                            if tool_call_id and not in_tool_call:
                                                in_tool_call = True
                                                current_tool_call = {
                                                    "id": tool_call_id,
                                                    "type": "function",
                                                    "function": {
                                                        "name": "",
                                                        "arguments": ""
                                                    }
                                                }
                                                tool_calls_buffer.append(current_tool_call)

                                            # Add function name
                                            if 'function' in tool_call_delta:
                                                if 'name' in tool_call_delta['function']:
                                                    current_tool_call['function']['name'] = tool_call_delta['function'][
                                                        'name']

                                                # Add function arguments
                                                if 'arguments' in tool_call_delta['function']:
                                                    current_tool_call['function']['arguments'] += \
                                                        tool_call_delta['function']['arguments']

                                    # If this is a new turn (delta has only role), yield any complete tool call
                                    if 'role' in delta and len(delta) == 1 and in_tool_call:
                                        in_tool_call = False
                                        yield {"tool_calls": tool_calls_buffer}
                                        tool_calls_buffer = []

                                # Save KV cache ID if present
                                if "kv_cache_id" in data and settings.get("session_id"):
                                    try:
                                        # Get chat_manager from settings instead of importing
                                        chat_manager = settings.get("chat_manager")
                                        if chat_manager:
                                            await chat_manager.update_kv_cache(settings.get("session_id"),
                                                                               data["kv_cache_id"])
                                    except Exception as e:
                                        self.logger.error(f"Error updating KV cache: {str(e)}")

                            except json.JSONDecodeError as e:
                                self.logger.error(f"Failed to parse SSE data: {e}")
                                continue

                    # Send any remaining tool calls
                    if tool_calls_buffer:
                        yield {"tool_calls": tool_calls_buffer}
                        tool_calls_buffer = []
                        current_tool_call = None
                        in_tool_call = False

                    # التأكد من إرسال أي بيانات متبقية
                    if tool_calls_buffer:
                        yield {"tool_calls": tool_calls_buffer}

        except asyncio.TimeoutError:
            self.logger.error(f"DeepSeek API streaming timeout after {api_timeout}s")
            yield {
                "content": "استغرق الرد وقتًا طويلاً. يمكنك إعادة المحاولة أو زيارة موقع اكسادوا مباشرة على https://exaado.com."}

        except Exception as e:
            self.logger.error(f"Error in streaming response: {str(e)}", exc_info=True)
            yield {"content": "حدث خطأ أثناء معالجة الطلب. يرجى المحاولة مرة أخرى."}

    async def get_chat_prefix_completion(self, messages: List[Dict[str, Any]], settings: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """Get completion using Chat Prefix Completion (Beta)"""
        # 1. Get API key from database
        api_key = await self.get_deepseek_api_key()
        if not api_key:
            self.logger.error("DeepSeek API key not available for prefix completion")
            return None

        # 2. Validate input 'messages' based on documentation
        if not messages or not isinstance(messages, list):
            self.logger.error("Invalid 'messages' format for prefix completion.")
            return None

        last_message = messages[-1]
        if last_message.get("role") != "assistant" or not last_message.get("prefix"):
            self.logger.error("Last message must be 'assistant' role with 'prefix: True' for prefix completion.")
            return None

        # 3. Prepare headers and payload
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        payload = {
            "model": settings.get("model", self.default_model),
            "messages": messages,
            "temperature": settings.get("temperature", 0.1),
            "max_tokens": settings.get("max_tokens", 100),
        }
        if "stop" in settings:
            payload["stop"] = settings.get("stop")

        # 4. Setup connection timeout
        api_timeout = 15  # seconds
        start = time.time()

        # 5. Define API call function (using Beta URL)
        async def call_beta_api(session):
            async with session.post(
                    self.api_url_beta,
                    headers=headers,
                    json=payload,
                    timeout=api_timeout
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self.logger.error(f"DeepSeek Beta API error: {resp.status} – {text}")
                    return None, resp.status, text
                try:
                    data = await resp.json()
                    if "choices" in data and data["choices"] and "message" in data["choices"][0]:
                        assistant_message = data["choices"][0]["message"]
                        return assistant_message, 200, None
                    else:
                        self.logger.error(f"Unexpected response structure from Beta API: {data}")
                        return None, resp.status, "Unexpected response structure"
                except json.JSONDecodeError:
                    self.logger.error(f"Failed to decode JSON from Beta API: {text}")
                    return None, resp.status, "Invalid JSON response"

        # 6. Execute API call
        try:
            async with aiohttp.ClientSession() as temp_sess:
                content, status, err = await call_beta_api(temp_sess)

            # 7. Handle response
            if status == 200 and content is not None:
                elapsed = time.time() - start
                self.logger.info(f"AI Beta API response time: {elapsed:.2f}s")
                return content
            else:
                self.logger.error(f"Failed to get prefix completion. Status: {status}, Error: {err}")
                return None

        except asyncio.TimeoutError:
            self.logger.error(f"DeepSeek Beta API timeout after {api_timeout}s")
            return None
        except Exception as e:
            self.logger.error(f"Error connecting to DeepSeek Beta API: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None

class AIModelManager:
    """إدارة نماذج الذكاء الاصطناعي المتعددة"""

    def __init__(self):
        self.models = {
            "deepseek": DeepSeekService(),
            # يمكن إضافة نماذج أخرى هنا مثل ChatGPT أو LLaMA أو غيرها
        }
        self.default_model = "deepseek"

    async def get_response(self, prompt, model=None, settings=None):
        """الحصول على رد من نموذج محدد أو النموذج الافتراضي"""
        model_name = model or self.default_model

        if model_name not in self.models:
            current_app.logger.warning(f"النموذج {model_name} غير متوفر، استخدام النموذج الافتراضي بدلاً منه")
            model_name = self.default_model

        model_instance = self.models[model_name]
        return await model_instance.get_response(prompt, settings)


    async def get_response(self, messages: List[Dict[str, str]], settings: dict = None) -> Dict[str, Any]:
        """
        Get a response from DeepSeek API with:
         - Caching
         - API timeout
         - Unified or temporary aiohttp session
         - Fallback strategy with appropriate messages
        """
        # 1. Get API key
        api_key = await get_deepseek_api_key()
        if not api_key:
            current_app.logger.error("DeepSeek API key not available")
            return {"role": "assistant", "content": "عذرًا، لا يمكن الاتصال بخدمة الذكاء الاصطناعي حاليًا."}

        # 2. Ensure valid settings
        if not isinstance(settings, dict):
            settings = {}

        # Update model parameters
        temperature = float(settings.get("temperature", 0.1))
        max_tokens = int(settings.get("max_tokens", 500))
        model = settings.get("model", self.default_model)

        # 3. Check for excessive length in messages
        total_length = sum(len(msg.get("content", "")) for msg in messages)
        if total_length > 32000:  # Typical context window limit
            current_app.logger.info("Total messages too large, trimming")
            # Trim user messages while preserving system messages
            system_messages = [m for m in messages if m.get("role") == "system"]
            user_assistant_messages = [m for m in messages if m.get("role") != "system"]

            # Keep most recent messages
            preserved_messages = user_assistant_messages[-10:]  # Keep last 10 exchanges
            messages = system_messages + preserved_messages

        # 4. Generate cache key and check cache
        cache_key = self._generate_cache_key(messages, temperature, max_tokens, model)
        cached = self._get_from_cache(cache_key)
        if cached:
            current_app.logger.info("Retrieved response from cache")
            return cached

        # 5. Prepare headers and payload
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        # Add function calling if provided
        if settings.get("tools"):
            payload["tools"] = settings.get("tools")

        if settings.get("tool_choice"):
            payload["tool_choice"] = settings.get("tool_choice")

        # Add reasoning_step if enabled
        if settings.get("reasoning_enabled"):
            payload["reasoning_step"] = settings.get("reasoning_steps", 3)

        # Add KV cache if available
        if settings.get("kv_cache_id"):
            payload["kv_cache_id"] = settings.get("kv_cache_id")

        # 6. Setup connection timeout
        api_timeout = 30  # seconds
        start = time.time()

        async def call_api(session):
            async with session.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=api_timeout
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    current_app.logger.error(f"DeepSeek API error: {resp.status} – {text}")
                    return None, resp.status, text
                data = await resp.json()
                assistant_message = data["choices"][0]["message"]

                # Save KV cache ID if present
                if "kv_cache_id" in data and settings.get("session_id"):
                    try:
                        from chatbot.chat_manager import ChatManager
                        chat_manager = ChatManager()
                        await chat_manager.update_kv_cache(settings.get("session_id"), data["kv_cache_id"])
                    except Exception as e:
                        current_app.logger.error(f"Error updating KV cache: {str(e)}")

                return assistant_message, 200, None

        try:
            # 7. Use elevated session or create temporary one
            session = getattr(current_app, "aiohttp_session", None)
            if session:
                content, status, err = await call_api(session)
            else:
                async with aiohttp.ClientSession() as temp_sess:
                    content, status, err = await call_api(temp_sess)

            # 8. Handle response
            if status == 200 and content is not None:
                elapsed = time.time() - start
                current_app.logger.info(f"AI API response time: {elapsed:.2f}s")
                self._store_in_cache(cache_key, content)
                return content
            else:
                # Error in API response
                # If asking about Exaado official page
                low_content = " ".join([m.get("content", "") for m in messages]).lower()
                if "صفحة" in low_content and "اكسادوا" in low_content and any(
                        k in low_content for k in ("رسميه", "رسمية", "عنوان", "رابط")):
                    return {"role": "assistant", "content": "صفحة اكسادوا الرسمية متاحة على الرابط: https://exaado.com"}
                return {
                    "role": "assistant",
                    "content": "عذرًا، لم أتمكن من العثور على إجابة دقيقة. للحصول على مساعدة في اكسادوا، يمكنك زيارة https://exaado.com أو التواصل مع فريق الدعم."
                }

        except asyncio.TimeoutError:
            current_app.logger.error(f"DeepSeek API timeout after {api_timeout}s")
            # Timeout case with question about official link
            low_content = " ".join([m.get("content", "") for m in messages]).lower()
            if "صفحة" in low_content and "اكسادوا" in low_content and any(
                    k in low_content for k in ("رسميه", "رسمية", "عنوان", "رابط")):
                return {"role": "assistant", "content": "صفحة اكسادوا الرسمية متاحة على الرابط: https://exaado.com"}
            return {"role": "assistant",
                    "content": "استغرق الرد وقتًا طويلاً. يمكنك زيارة موقع اكسادوا مباشرة على https://exaado.com."}

        except Exception as e:
            current_app.logger.error(f"Error connecting to DeepSeek API: {e}")
            # Try to handle common errors
            if "صفحة" in " ".join([m.get("content", "") for m in messages]).lower() and "اكسادوا" in " ".join(
                    [m.get("content", "") for m in messages]).lower():
                return {"role": "assistant", "content": "صفحة اكسادوا الرسمية متاحة على الرابط: https://exaado.com"}
            return {"role": "assistant",
                    "content": "حدث خطأ أثناء الاتصال بخدمة الذكاء الاصطناعي. يمكنك زيارة موقع اكسادوا مباشرة على https://exaado.com."}


    def _get_from_cache(self, key):
        """Retrieve response from cache if exists and valid"""
        if key in self.cache:
            cache_item = self.cache[key]
            current_time = time.time()

            # Check item validity
            if current_time - cache_item['timestamp'] < self.cache_ttl:
                return cache_item['response']
            else:
                # Remove expired item
                del self.cache[key]

        return None

    def _store_in_cache(self, key, response):
        """Store response in cache"""
        self.cache[key] = {
            'response': response,
            'timestamp': time.time()
        }

        # Clean old items from cache
        self._clean_cache()

        def _generate_cache_key(self, messages, temperature, max_tokens, model):
            """Create a cache key from messages and settings"""
            # Create a simplified version of messages for caching
            simplified_messages = []
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                # Only include the first 100 chars of content for the key
                simplified_msg = f"{role}:{content[:100]}"
                simplified_messages.append(simplified_msg)

            # Join all messages with a delimiter
            message_str = "|||".join(simplified_messages)
            message_hash = str(hash(message_str))

            return f"{message_hash}_{temperature}_{max_tokens}_{model}"

        def _clean_cache(self):
            """Clean cache of old items"""
            current_time = time.time()
            keys_to_remove = []

            for key, item in self.cache.items():
                if current_time - item['timestamp'] >= self.cache_ttl:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                del self.cache[key]

            # If cache is too large, remove oldest items
            max_cache_size = 1000  # Maximum number of items in cache
            if len(self.cache) > max_cache_size:
                # Sort items by time and remove oldest
                sorted_items = sorted(self.cache.items(), key=lambda x: x[1]['timestamp'])
                items_to_remove = len(self.cache) - max_cache_size

                for i in range(items_to_remove):
                    del self.cache[sorted_items[i][0]]
