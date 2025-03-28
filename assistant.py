import os
import json
import requests
from typing import List, Dict

class DeepSeekDevAssistant:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.deepseek.com/v1/chat/completions"
        self.session_messages = []
        self.current_context = []
        self.max_token_chunk = 4000  # الحد الأقصى الآمن لكل طلب

    def _call_api(self, messages: List[Dict]) -> str:
        """الدالة الأساسية للتواصل مع API"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 0.3
        }

        try:
            response = requests.post(self.base_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        
        except Exception as e:
            return f"Error: {str(e)}"

    def _chunk_text(self, text: str) -> List[str]:
        """تقسيم النص إلى أجزاء حسب الحد الأقصى للtokens"""
        return [text[i:i+self.max_token_chunk] for i in range(0, len(text), self.max_token_chunk)]

    def analyze_code(self, file_path: str):
        """تحليل ملف كود وإضافة السياق للجلسة"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                chunks = self._chunk_text(content)
                
                for chunk in chunks:
                    self.current_context.append({
                        'role': 'system',
                        'content': f"Current code file ({os.path.basename(file_path)}):\n{chunk}"
                    })
                
                return f"تم تحميل ملف {file_path} ({len(content)} حرف)"
        
        except Exception as e:
            return f"خطأ في قراءة الملف: {str(e)}"

    def process_command(self, command: str):
        """معالجة الأوامر الخاصة"""
        # تحديث السياق مع الحفاظ على الحد الأقصى للسياق
        self.current_context = self.current_context[-8:]  # الاحتفاظ بآخر 8 رسائل كسياق
        
        # إرسال الطلب مع السياق التاريخي
        full_messages = [
            {"role": "system", "content": "أنت مساعد مبرمج خبير. قم بتحليل الكود واقترح تحسينات مع أمثلة عملية."},
            *self.current_context,
            {"role": "user", "content": command}
        ]
        
        response = self._call_api(full_messages)
        
        # تحديث السياق بالرد
        self.current_context.extend([
            {"role": "user", "content": command},
            {"role": "assistant", "content": response}
        ])
        
        return response

    def save_changes(self, new_code: str, file_path: str):
        """حفظ التعديلات المقترحة"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_code)
            return f"تم حفظ التعديلات بنجاح في {file_path}"
        except Exception as e:
            return f"خطأ في الحفظ: {str(e)}"

class UserInterface:
    def __init__(self, assistant: DeepSeekDevAssistant):
        self.assistant = assistant
        self.current_file = None

    def start(self):
        """تشغيل الواجهة التفاعلية"""
        print("""
        === مساعد تطوير البرمجيات ===
        الأوامر المتاحة:
        /load <مسار الملف> - تحميل ملف كود
        /save <مسار الملف> - حفظ التعديلات
        /exit - الخروج
        """)
        
        while True:
            user_input = input("\n>>> ")
            
            if user_input.startswith('/'):
                self._handle_command(user_input)
            else:
                self._handle_chat(user_input)

    def _handle_command(self, command: str):
        """معالجة الأوامر الخاصة"""
        parts = command.split()
        
        if parts[0] == '/load' and len(parts) > 1:
            result = self.assistant.analyze_code(parts[1])
            self.current_file = parts[1]
            print(result)
            
        elif parts[0] == '/save' and len(parts) > 1:
            if not self.current_file:
                print("الرجاء تحميل ملف أولاً باستخدام /load")
                return
                
            response = self.assistant.process_command("يرجى إخراج الكود المعدل فقط دون أي شرح إضافي")
            save_result = self.assistant.save_changes(response, parts[1])
            print(save_result)
            
        elif parts[0] == '/exit':
            print("وداعاً!")
            exit()
            
        else:
            print("أمر غير معروف")

    def _handle_chat(self, message: str):
        """معالجة الرسائل العادية"""
        response = self.assistant.process_command(message)
        print("\nالمساعد:", response)

if __name__ == "__main__":
    API_KEY = "sk-6f828a6b26234bcfb2df6121fea97ef5"  # استبدال بمفتاح API الحقيقي
    
    assistant = DeepSeekDevAssistant(API_KEY)
    interface = UserInterface(assistant)
    interface.start()