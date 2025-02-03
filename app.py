import asyncpg
import logging
import os
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from backend.telegram_payments import setup_payment_handlers  # <-- استيراد معالجات الدفع الجديدة
from backend.telegram_bot import init_bot, start_telegram_bot, setup_webhook
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session
from Crypto.Signature import pkcs1_15
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256

# 🔹 إنشاء التطبيق
app = Quart(__name__)
app = cors(app, allow_origin="*")  # تمكين CORS لجميع الطلبات

# 🔹 إعداد تسجيل الأخطاء والمعلومات
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# 🔹 تحميل المفتاح الخاص من متغير البيئة
private_key_content = os.environ.get("PRIVATE_KEY")
if not private_key_content:
    raise ValueError("PRIVATE_KEY environment variable is not set.")

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
        app.db_pool = await asyncpg.create_pool(**DATABASE_CONFIG)
        logging.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")

        await setup_scheduler()
        await init_bot()
        await setup_webhook()  # تمت الإضافة
        await start_telegram_bot(setup_payment_handlers)

    except Exception as e:
        logging.error(f"❌ خطأ أثناء الاتصال بقاعدة البيانات: {e}")

# 🔹 إغلاق الموارد عند إيقاف التطبيق
@app.after_serving
async def close_resources():
    try:
        await close_telegram_bot_session()
        await app.db_pool.close()
        logging.info("✅ تم إغلاق جميع الموارد بنجاح.")
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إغلاق الموارد: {e}")

# 🔹 نقطة نهاية اختبارية
@app.route("/")
async def home():
    return "Hello, Quart!"

# 🔹 تشغيل التطبيق
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)