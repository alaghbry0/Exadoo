import asyncio
import os
from aiogram import Bot
from dotenv import load_dotenv

# تحميل متغيرات البيئة من ملف .env
load_dotenv()

# استيراد توكن البوت
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    print("❌ خطأ: لم يتم العثور على TELEGRAM_BOT_TOKEN في ملف .env")
    exit()

async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    print("🔄 محاولة حذف الويب هوك الحالي...")
    try:
        # drop_pending_updates=True تقوم بحذف أي تحديثات عالقة لم تتم معالجتها
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ تم حذف الويب هوك بنجاح!")
        print("👍 الآن يمكنك تشغيل تطبيقك الرئيسي بأمان.")
    except Exception as e:
        print(f"❌ حدث خطأ أثناء حذف الويب هوك: {e}")
    finally:
        # إغلاق جلسة البوت
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())