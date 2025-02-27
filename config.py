import os
import asyncpg
from dotenv import load_dotenv

# تحميل متغيرات البيئة من .env
load_dotenv()


# 🔹 إعداد اتصال قاعدة البيانات
DATABASE_CONFIG = {
    'user': os.getenv('DB_USER', 'neondb_owner'),
    'password': os.getenv('DB_PASSWORD', 'npg_hqkR5UfFX'),
    'database': os.getenv('DB_NAME', 'neondb'),
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 5432)),
}

DATABASE_URI = f"postgresql://{DATABASE_CONFIG['user']}:{DATABASE_CONFIG['password']}@{DATABASE_CONFIG['host']}:{DATABASE_CONFIG['port']}/{DATABASE_CONFIG['database']}"


# 🔹 مفتاح بوت تيليجرام
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"  # 🔹 توليد رابط API تلقائيًا

# 🔹 تحميل المفتاح الخاص للتوقيع
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# 🔹 متغيرات أخرى للتحكم في الإعدادات
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # مستوى السجلات
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"  # تمكين وضع التصحيح

GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID")
SECRET_KEY=os.getenv("SECRET_KEY")

REFRESH_SECRET_KEY =os.getenv("refresh_secret_key") # تأكد من تغييره إلى قيمة آمنة
