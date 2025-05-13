import os
import random
import uuid
import json
import time
from locust import HttpUser, task, between

class ApiUser(HttpUser):
    """
    مستخدم افتراضي يختبر نقاط النهاية في التطبيق.
    """
    # وقت الانتظار العشوائي بين تنفيذ المهام (بالثواني)
    # يحاكي سلوك المستخدم الحقيقي الذي لا يرسل طلبات بشكل مستمر
    wait_time = between(1, 3) # انتظر بين 1 و 3 ثواني بين المهام

    def on_start(self):
        """
        يُنفذ مرة واحدة عند بدء تشغيل المستخدم الافتراضي.
        يستخدم لقراءة الإعدادات أو تنفيذ تسجيل الدخول (إذا لزم الأمر).
        """
        self.webhook_secret = os.getenv("WEBHOOK_SECRET")
        if not self.webhook_secret:
            print("❌ خطأ: متغير البيئة WEBHOOK_SECRET غير مضبوط. قم بتعيينه قبل تشغيل Locust.")
            # يمكنك إيقاف الاختبار هنا إذا كان الـ secret ضرورياً لكل الطلبات
            # self.environment.runner.quit()
            # أو السماح للمستخدم بمواصلة تنفيذ المهام التي لا تحتاج للـ secret
            pass

        self.headers = {
            'Content-Type': 'application/json'
            # سيتم إضافة Authorization ديناميكياً في المهام التي تحتاجه
        }

    @task(1) # (1) يشير إلى وزن المهمة، المهام ذات الوزن الأعلى تُنفذ أكثر
    def check_home(self):
        """
        مهمة بسيطة للتحقق من نقطة النهاية الرئيسية (فحص صحة).
        """
        self.client.get("/", name="Check Homepage / Health") # name يستخدم لتجميع النتائج في واجهة Locust

    @task(5) # هذه المهمة أهم، لذا نعطيها وزناً أعلى (تُنفذ 5 مرات مقابل مرة واحدة لـ check_home)
    def attempt_subscribe(self):
        """
        محاكاة عملية اشتراك عن طريق استدعاء /api/subscribe.
        """
        if not self.webhook_secret:
            # لا يمكن تنفيذ هذه المهمة بدون الـ secret
            print("⚠️ تخطي مهمة الاشتراك - WEBHOOK_SECRET غير موجود")
            return

        # توليد بيانات ديناميكية لكل طلب محاكاة
        # استخدم نطاقاً واسعاً لتجنب التضارب قدر الإمكان، أو قم بتنسيقه بناءً على معرفتك بالبيانات
        test_telegram_id = random.randint(1000000000, 9999999999)
        test_plan_id = random.randint(1, 5) # افترض أن لديك خطط من 1 إلى 5
        test_payment_id = uuid.uuid4().hex # معرف دفع فريد (مثل tx_hash)
        test_payment_token = uuid.uuid4().hex # رمز دفع فريد
        test_username = f"locust_user_{test_telegram_id}"
        test_full_name = f"Locust Test User {test_telegram_id}"

        payload = {
            "telegram_id": test_telegram_id,
            "subscription_plan_id": test_plan_id,
            "payment_id": test_payment_id,
            "payment_token": test_payment_token,
            "username": test_username,
            "full_name": test_full_name
        }

        # إضافة رأس المصادقة
        auth_headers = self.headers.copy()
        auth_headers['Authorization'] = f'Bearer {self.webhook_secret}'

        # إرسال طلب POST إلى نقطة نهاية الاشتراك
        # name="/api/subscribe" مهم لتجميع الإحصائيات بشكل صحيح في Locust UI
        with self.client.post("/api/subscribe",
                              headers=auth_headers,
                              json=payload,
                              name="/api/subscribe",
                              catch_response=True) as response:
            # يمكنك إضافة تحقق من الاستجابة هنا إذا أردت
            # catch_response=True يسمح لك بفحص الاستجابة قبل أن يقرر Locust نجاحها أو فشلها
            try:
                response_data = response.json()
                if response.status_code == 200 and "message" in response_data:
                    response.success() # اعتبر الطلب ناجحاً
                    # يمكنك طباعة رسالة نجاح بشكل متقطع لتأكيد العمل
                    # if random.random() < 0.01: # اطبع 1% من الوقت
                    #    print(f"✅ Subscription successful for TG ID {test_telegram_id}")
                elif response.status_code == 400 and "error" in response_data:
                    # قد يكون خطأ 400 متوقعاً إذا كانت البيانات غير صالحة (مثل خطة غير موجودة)
                    # يمكنك اعتباره فشلاً أو نجاحاً حسب منطق الاختبار
                    response.failure(f"API returned 400: {response_data.get('error', 'Unknown error')}")
                elif response.status_code == 403:
                    response.failure("Authorization failed (403). Check WEBHOOK_SECRET.")
                elif response.status_code == 500:
                    response.failure("Server error (500)")
                    # طباعة تفاصيل أكثر للمساعدة في التشخيص
                    print(f"❌ Server Error 500 for TG ID {test_telegram_id}. Response: {response.text[:200]}") # اطبع أول 200 حرف
                else:
                    # أي حالة أخرى تعتبر فشلاً
                    response.failure(f"Unexpected status code: {response.status_code}")

            except json.JSONDecodeError:
                response.failure("Invalid JSON response")
            except Exception as e:
                response.failure(f"Exception during response handling: {str(e)}")


# إذا كنت تريد تشغيل Locust من سطر الأوامر بدون واجهة المستخدم (نادراً ما يستخدم للمراقبة الأولية)
# يمكنك إلغاء التعليق عن الكود التالي وتعديله
# if __name__ == "__main__":
#     import sys
#     # مثال لتشغيل Locust من الكود مباشرة
#     # يتطلب تثبيت gevent: pip install gevent
#     from locust.env import Environment
#     from locust.stats import stats_printer, stats_history
#     from locust.log import setup_logging
#
#     setup_logging("INFO", None)
#
#     env = Environment(user_classes=[ApiUser])
#     env.create_local_runner()
#
#     # ابدأ تشغيل مهمة طباعة الإحصائيات في الخلفية
#     gevent.spawn(stats_printer(env.stats))
#
#     # ابدأ تشغيل مهمة حفظ سجل الإحصائيات إلى ملف CSV
#     gevent.spawn(stats_history, env.runner)
#
#     # ابدأ الاختبار
#     # قم بتغيير عدد المستخدمين ومعدل الإطلاق حسب الحاجة
#     env.runner.start(user_count=10, spawn_rate=2) # 10 مستخدمين، إطلاق مستخدمين جدد كل ثانية
#
#     # اسمح للاختبار بالعمل لمدة محددة (مثلاً 60 ثانية)
#     gevent.spawn_later(60, lambda: env.runner.quit())
#
#     # انتظر حتى ينتهي الاختبار
#     env.runner.greenlet.join()