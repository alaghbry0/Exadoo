import aiohttp
import os
import json
from quart import current_app


class DeepSeekService:
    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        self.default_model = "deepseek-chat"  # استبدل بالنموذج المتاح لديك

    async def get_response(self, prompt, settings=None):
        """الحصول على رد من DeepSeek API"""
        if not self.api_key:
            current_app.logger.error("مفتاح API للـ DeepSeek غير متوفر")
            return "عذرًا، لا يمكن الاتصال بخدمة الذكاء الاصطناعي حاليًا."

        settings = settings or {}
        temperature = settings.get('temperature', 0.1)
        max_tokens = settings.get('max_tokens', 500)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        payload = {
            "model": self.default_model,
            "messages": [
                {"role": "system", "content": "أنت مساعد دعم العملاء لشركة اكسادوا، تجيب بشكل دقيق ومهني."},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        try:
            # استخدام جلسة aiohttp من التطبيق الرئيسي إذا كانت متاحة
            session = current_app.aiohttp_session if hasattr(current_app, 'aiohttp_session') else None

            if session:
                async with session.post(self.api_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result["choices"][0]["message"]["content"]
                    else:
                        error_text = await response.text()
                        current_app.logger.error(f"خطأ في DeepSeek API: {response.status} - {error_text}")
                        return "حدث خطأ أثناء معالجة طلبك. الرجاء المحاولة مرة أخرى."
            else:
                # إنشاء جلسة مؤقتة إذا لم تكن متاحة في التطبيق
                async with aiohttp.ClientSession() as temp_session:
                    async with temp_session.post(self.api_url, headers=headers, json=payload) as response:
                        if response.status == 200:
                            result = await response.json()
                            return result["choices"][0]["message"]["content"]
                        else:
                            error_text = await response.text()
                            current_app.logger.error(f"خطأ في DeepSeek API: {response.status} - {error_text}")
                            return "حدث خطأ أثناء معالجة طلبك. الرجاء المحاولة مرة أخرى."

        except Exception as e:
            current_app.logger.error(f"خطأ في الاتصال بـ DeepSeek API: {str(e)}")
            return "حدث خطأ أثناء الاتصال بخدمة الذكاء الاصطناعي. الرجاء المحاولة مرة أخرى."