import asyncpg
import logging
import os
import aiohttp  # ✅ استيراد aiohttp
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from routes.telegram_webhook import payments_bp
from backend.telegram_bot import init_bot, start_telegram_bot, setup_webhook, close_bot_session
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session
from Crypto.Signature import pkcs1_15
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256

# ✅ التحقق من متغيرات البيئة الأساسية قبل تشغيل التطبيق
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "PORT"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط. الرجاء التأكد من الإعدادات.")

# 🔹 إنشاء التطبيق
app = Quart(__name__)

# ✅ جلسة aiohttp عامة يمكن استخدامها في كل مكان
app.aiohttp_session = None

# 🔹 ضبط CORS للسماح بمصادر محددة فقط
ALLOWED_ORIGINS = ["https://exadoo.onrender.com", "https://telegram.org"]
app = cors(app, allow_origin=ALLOWED_ORIGINS)

# 🔹 إعداد تسجيل الأخطاء والمعلومات
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# 🔹 تحميل المفتاح الخاص
private_key_content = os.environ.get("PRIVATE_KEY")
private_key = RSA.import_key(private_key_content)

# 🔹 توقيع بيانات وهمية (للاختبار فقط)
message = b"transaction data"
hash_msg = SHA256.new(message)
signature = pkcs1_15.new(private_key).sign(hash_msg)
logging.info(f"✅ تم توقيع البيانات بنجاح: {signature.hex()}")

# 🔹 تسجيل نقاط API
app.register_blueprint(subscriptions_bp)
app.register_blueprint(user_bp)
app.register_blueprint(shop)
app.register_blueprint(payments_bp)

# 🔹 وظيفة تشغيل الجدولة
async def setup_scheduler():
    logging.info("📅 بدء تشغيل الجدولة...")
    try:
        await start_scheduler(app.db_pool)
        logging.info("✅ تمت جدولة المهام بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إعداد الجدولة: {e}")

# 🔹 الاتصال بقاعدة البيانات قبل تشغيل التطبيق
@app.before_serving
async def create_db_connection():
    try:
        logging.info("🔄 جاري الاتصال بقاعدة البيانات...")
        app.db_pool = await asyncpg.create_pool(**DATABASE_CONFIG)
        app.aiohttp_session = aiohttp.ClientSession()  # ✅ إنشاء جلسة aiohttp عند بدء التطبيق
        logging.info("✅ تم الاتصال بقاعدة البيانات وإنشاء جلسة aiohttp بنجاح.")

        await setup_scheduler()
        await init_bot()
        await setup_webhook()
        await start_telegram_bot()

    except asyncpg.exceptions.PostgresError as e:
        logging.critical(f"🚨 فشل الاتصال بقاعدة البيانات: {e}")
        raise RuntimeError("🚨 فشل بدء التطبيق بسبب مشكلة في قاعدة البيانات.") from e

    except Exception as e:
        logging.error(f"❌ خطأ أثناء الاتصال بقاعدة البيانات أو بدء الخدمات: {e}")
        raise RuntimeError("❌ حدث خطأ أثناء تشغيل التطبيق.") from e

# 🔹 إغلاق الموارد عند إيقاف التطبيق
@app.after_serving
async def close_resources():
    try:
        logging.info("🔄 جاري إغلاق الجلسات المفتوحة...")
        await close_bot_session()  # ✅ إغلاق جلسة بوت تيليجرام هنا
        await close_telegram_bot_session()

        # ✅ إغلاق جلسة aiohttp أولاً
        if app.aiohttp_session:
            await app.aiohttp_session.close()
            logging.info("✅ تم إغلاق جميع جلسات aiohttp بنجاح.")

        # 🔹 إغلاق اتصال قاعدة البيانات بعد ذلك
        if app.db_pool:
            await app.db_pool.close()
            logging.info("✅ تم إغلاق اتصال قاعدة البيانات بنجاح.")

    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق الموارد: {e}")

# 🔹 نقطة نهاية اختبارية
@app.route("/")
async def home():
    return "🚀 Exadoo API is running!"

# 🔹 تشغيل التطبيق
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))  # ✅ تحديد المنفذ تلقائيًا من متغير البيئة
    logging.info(f"🚀 تشغيل Exadoo API على المنفذ {port}...")
    app.run(debug=False, host="0.0.0.0", port=port)