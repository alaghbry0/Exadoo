import aiohttp
import os
import json
from quart import current_app
import time
import numpy as np
import re
from typing import Dict, Any, List, Optional


class DeepSeekService:
    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        self.default_model = "deepseek-chat"  # استبدل بالنموذج المتاح لديك
        self.cache = {}  # تخزين مؤقت بسيط للاستجابات المتكررة
        self.cache_ttl = 3600  # مدة صلاحية التخزين المؤقت (بالثواني)

    async def get_response(self, prompt, settings=None):
        """الحصول على رد من DeepSeek API مع التخزين المؤقت"""
        api_key = await get_deepseek_api_key()
        if not api_key:
            current_app.logger.error("مفتاح API للـ DeepSeek غير متوفر")
            return "عذرًا، لا يمكن الاتصال بخدمة الذكاء الاصطناعي حاليًا."

        # التأكد من أن settings قاموس
        if settings is None or not isinstance(settings, dict):
            settings = {}

        temperature = settings.get('temperature', 0.1)
        max_tokens = settings.get('max_tokens', 500)
        model = settings.get('model', self.default_model)

        # تنظيف المطلب لتجنب استخدامه كمفتاح حرفياً في التخزين المؤقت
        cache_key = self._generate_cache_key(prompt, temperature, max_tokens, model)

        # التحقق من التخزين المؤقت
        cached_response = self._get_from_cache(cache_key)
        if cached_response:
            current_app.logger.info("تم استرجاع الرد من التخزين المؤقت")
            return cached_response

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        # بناء الرسائل وإضافة سياق النظام إذا كان موجوداً في الإعدادات
        messages = []
        system_prompt = settings.get('system_prompt', "أنت مساعد دعم العملاء لشركة اكسادوا، تجيب بشكل دقيق ومهني.")

        messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        try:
            # استخدام جلسة aiohttp من التطبيق الرئيسي إذا كانت متاحة
            session = current_app.aiohttp_session if hasattr(current_app, 'aiohttp_session') else None

            start_time = time.time()

            if session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=30) as response:
                    if response.status == 200:
                        result = await response.json()
                        response_text = result["choices"][0]["message"]["content"]

                        # قياس وقت الاستجابة
                        response_time = time.time() - start_time
                        current_app.logger.info(f"وقت استجابة AI API: {response_time:.2f} ثانية")

                        # تخزين الرد في التخزين المؤقت
                        self._store_in_cache(cache_key, response_text)

                        return response_text
                    else:
                        error_text = await response.text()
                        current_app.logger.error(f"خطأ في DeepSeek API: {response.status} - {error_text}")
                        return "حدث خطأ أثناء معالجة طلبك. الرجاء المحاولة مرة أخرى."
            else:
                # إنشاء جلسة مؤقتة إذا لم تكن متاحة في التطبيق
                async with aiohttp.ClientSession() as temp_session:
                    async with temp_session.post(self.api_url, headers=headers, json=payload, timeout=30) as response:
                        if response.status == 200:
                            result = await response.json()
                            response_text = result["choices"][0]["message"]["content"]

                            # قياس وقت الاستجابة
                            response_time = time.time() - start_time
                            current_app.logger.info(f"وقت استجابة AI API: {response_time:.2f} ثانية")

                            # تخزين الرد في التخزين المؤقت
                            self._store_in_cache(cache_key, response_text)

                            return response_text
                        else:
                            error_text = await response.text()
                            current_app.logger.error(f"خطأ في DeepSeek API: {response.status} - {error_text}")
                            return "حدث خطأ أثناء معالجة طلبك. الرجاء المحاولة مرة أخرى."

        except asyncio.TimeoutError:
            current_app.logger.error("انتهت مهلة الاتصال بـ DeepSeek API")
            return "استغرق الرد وقتًا طويلاً. الرجاء المحاولة مرة أخرى لاحقًا."

        except Exception as e:
            current_app.logger.error(f"خطأ في الاتصال بـ DeepSeek API: {str(e)}")
            return "حدث خطأ أثناء الاتصال بخدمة الذكاء الاصطناعي. الرجاء المحاولة مرة أخرى."

    def _generate_cache_key(self, prompt, temperature, max_tokens, model):
        """إنشاء مفتاح تخزين مؤقت من المطلب والإعدادات"""
        # تبسيط المطلب للاستخدام كمفتاح
        simplified_prompt = re.sub(r'\s+', ' ', prompt).strip().lower()
        # استخدم الـ 100 حرف الأولى فقط للمفتاح + هاش للمطلب الكامل
        prompt_hash = str(hash(simplified_prompt))
        short_key = simplified_prompt[:100] + '_' + prompt_hash
        return f"{short_key}_{temperature}_{max_tokens}_{model}"

    def _store_in_cache(self, key, response):
        """تخزين الرد في التخزين المؤقت"""
        self.cache[key] = {
            'response': response,
            'timestamp': time.time()
        }

        # تنظيف التخزين المؤقت من العناصر القديمة
        self._clean_cache()

    def _get_from_cache(self, key):
        """استرجاع الرد من التخزين المؤقت إذا كان موجودًا وساري المفعول"""
        if key in self.cache:
            cache_item = self.cache[key]
            current_time = time.time()

            # التحقق من صلاحية العنصر المخزن
            if current_time - cache_item['timestamp'] < self.cache_ttl:
                return cache_item['response']
            else:
                # إزالة العنصر منتهي الصلاحية
                del self.cache[key]

        return None

    def _clean_cache(self):
        """تنظيف التخزين المؤقت من العناصر القديمة"""
        current_time = time.time()
        keys_to_remove = []

        for key, item in self.cache.items():
            if current_time - item['timestamp'] >= self.cache_ttl:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self.cache[key]

        # إذا كان حجم التخزين المؤقت كبيرًا جدًا، قم بإزالة أقدم العناصر
        max_cache_size = 1000  # عدد العناصر الأقصى في التخزين المؤقت
        if len(self.cache) > max_cache_size:
            # ترتيب العناصر حسب الوقت وإزالة الأقدم
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