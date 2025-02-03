import asyncpg
import logging
import os
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from routes.telegram_webhook import payments_bp
from backend.telegram_payments import setup_payment_handlers  # <-- استيراد معالجات الدفع الجديدة
from backend.telegram_bot import init_bot, start_telegram_bot, setup_webhook
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session
from Crypto.Signature import pkcs1_15
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256

# ✅ التحقق من متغيرات البيئة الأساسية قبل تشغيل التطبيق
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط. الرجاء التأكد من الإعدادات.")

# 🔹 إنشاء التطبيق
app = Quart(__name__)

# 🔹 ضبط CORS للسماح بمصادر محددة فقط
ALLOWED_ORIGINS = ["https://exadoo.onrender.com", "https://telegram.org"]
app = cors(app, allow_origin=ALLOWED_ORIGINS)

# 🔹 إعداد تسجيل الأخطاء والمعلومات
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# 🔹 تحميل المفتاح الخاص
private_key_content = os.environ.get("PRIVATE_KEY")
private_key = RSA.import_key(private_key_content)

# 🔹 توقيع بيانات وهمية (للاختبار فقط)
message = b"transaction data"
hash_msg = SHA256.new(message)
signature = pkcs1_15.new(private_key).sign(hash_msg)
logging.info(f"✅ Signed message: {signature.hex()}")

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
        logging.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")

        await setup_scheduler()
        await init_bot()
        await setup_webhook()
        await start_telegram_bot(setup_payment_handlers)

    except asyncpg.exceptions.PostgresError as e:
        logging.critical(f"🚨 فشل الاتصال بقاعدة البيانات: {e}")
        raise RuntimeError("🚨 فشل بدء التطبيق بسبب مشكلة في قاعدة البيانات.") from e

    except Exception as e:
        logging.error(f"❌ خطأ أثناء الاتصال بقاعدة البيانات أو بدء الخدمات: {e}")

# 🔹 إغلاق الموارد عند إيقاف التطبيق
@app.after_serving
async def close_resources():
    try:
        logging.info("🔄 جاري إغلاق الجلسات المفتوحة...")
        await close_telegram_bot_session()

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
    logging.info("🚀 تشغيل Exadoo API...")
    app.run(debug=True, host="0.0.0.0", port=5000)
