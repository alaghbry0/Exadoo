# ai_service.py
import aiohttp
import os
import json
from quart import current_app
import time
import asyncio
import numpy as np
import re
from typing import Dict, Any, List, Optional


class DeepSeekService:
    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        # يمكن الاحتفاظ بالـ URL الأساسي لـ v1 هنا
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        # URL خاص بالبيتا لميزة إكمال البادئة
        self.api_url_beta = "https://api.deepseek.com/beta/chat/completions"
        self.default_model = "deepseek-chat"
        self.cache = {}
        self.cache_ttl = 3600

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

    async def get_chat_prefix_completion(self, messages: List[Dict[str, Any]], settings: Optional[Dict] = None) -> \
    Optional[Dict[str, Any]]:
        """
        Get a completion using Chat Prefix Completion (Beta).
        Follows the Chat Completion API structure.
        """
        if not settings:
            settings = {}

        # 1. Get API key
        api_key = await get_deepseek_api_key()  # افترض أن هذه الدالة موجودة وتعمل
        if not api_key:
            current_app.logger.error("DeepSeek API key not available for prefix completion")
            return None

        # 2. Validate input 'messages' based on documentation
        if not messages or not isinstance(messages, list):
            current_app.logger.error("Invalid 'messages' format for prefix completion.")
            return None
        last_message = messages[-1]
        if last_message.get("role") != "assistant" or not last_message.get("prefix"):
            current_app.logger.error("Last message must be 'assistant' role with 'prefix: True' for prefix completion.")
            # يمكنك محاولة تصحيحها تلقائياً أو إرجاع خطأ
            # للتصحيح التلقائي:
            # last_message['role'] = 'assistant'
            # last_message['prefix'] = True
            # ولكن الأفضل أن يتم تمريرها بشكل صحيح من البداية
            return None

        # 3. Prepare headers and payload
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        payload = {
            "model": settings.get("model", self.default_model),
            "messages": messages,  # استخدام قائمة الرسائل مباشرة
            "temperature": settings.get("temperature", 0.1),
            "max_tokens": settings.get("max_tokens", 100),
            # أضف 'stop' إذا كانت موجودة في settings
        }
        if "stop" in settings:
            payload["stop"] = settings.get("stop")

        # 4. Setup connection timeout
        api_timeout = 15  # seconds
        start = time.time()

        # 5. Define API call function (using Beta URL)
        async def call_beta_api(session):
            # استخدم self.api_url_beta هنا
            async with session.post(
                    self.api_url_beta,  # <-- URL البيتا
                    headers=headers,
                    json=payload,
                    timeout=api_timeout
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    current_app.logger.error(f"DeepSeek Beta API error: {resp.status} – {text}")
                    return None, resp.status, text
                try:
                    data = await resp.json()
                    # التحقق من وجود المفاتيح قبل الوصول إليها
                    if "choices" in data and data["choices"] and "message" in data["choices"][0]:
                        assistant_message = data["choices"][0]["message"]
                        return assistant_message, 200, None
                    else:
                        current_app.logger.error(f"Unexpected response structure from Beta API: {data}")
                        return None, resp.status, "Unexpected response structure"
                except json.JSONDecodeError:
                    current_app.logger.error(f"Failed to decode JSON from Beta API: {text}")
                    return None, resp.status, "Invalid JSON response"

        # 6. Execute API call
        try:
            session = getattr(current_app, "aiohttp_session", None)
            if session:
                content, status, err = await call_beta_api(session)
            else:
                async with aiohttp.ClientSession() as temp_sess:
                    content, status, err = await call_beta_api(temp_sess)

            # 7. Handle response
            if status == 200 and content is not None:
                elapsed = time.time() - start
                current_app.logger.info(f"AI Beta API response time: {elapsed:.2f}s")
                # لا حاجة للكاش هنا عادةً لإكمال البادئة، أو يمكن إضافته إذا لزم الأمر
                return content  # إرجاع رسالة المساعد المكتملة (قاموس)
            else:
                current_app.logger.error(f"Failed to get prefix completion. Status: {status}, Error: {err}")
                return None

        except asyncio.TimeoutError:
            current_app.logger.error(f"DeepSeek Beta API timeout after {api_timeout}s")
            return None
        except Exception as e:
            current_app.logger.error(f"Error connecting to DeepSeek Beta API: {e}")
            import traceback
            current_app.logger.error(traceback.format_exc())
            return None

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

    def _store_in_cache(self, key, response):
        """Store response in cache"""
        self.cache[key] = {
            'response': response,
            'timestamp': time.time()
        }

        # Clean old items from cache
        self._clean_cache()

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

# في ai_service.py
_api_key_cache = {
    "api_key": None,
    "timestamp": 0.0
}
API_KEY_CACHE_TTL = 300  # 5 دقائق


async def get_deepseek_api_key() -> Optional[str]:
    global _api_key_cache
    now = asyncio.get_event_loop().time()

    # إذا انتهت مدة التخزين المؤقت أو لا يوجد مفتاح
    if _api_key_cache["api_key"] is None or now - _api_key_cache["timestamp"] > API_KEY_CACHE_TTL:
        async with current_app.db_pool.acquire() as connection:
            try:
                record = await connection.fetchrow(
                    "SELECT api_key FROM wallet ORDER BY id DESC LIMIT 1"
                )
                if not record or not record['api_key']:
                    current_app.logger.error("❌ لا يوجد API Key مسجل في جدول المحفظة")
                    return None

                _api_key_cache["api_key"] = record['api_key']
                _api_key_cache["timestamp"] = now
                current_app.logger.info("تم تحديث API Key من قاعدة البيانات")
            except Exception as e:
                current_app.logger.error(f"خطأ في استرجاع API Key: {str(e)}")
                return None
    return _api_key_cache["api_key"]

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