
import asyncpg
import logging
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp  # ✅ استيراد aiohttp
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from routes.admin_routes import admin_routes
from routes.subscriptions_routs import public_routes
from routes.payment_confirmation import payment_confirmation_bp
#from routes.webhook import payments_bp
from routes.auth_routes import auth_routes
from telegram_bot import start_bot, bot, telegram_bot_bp  # ✅ استخدام `telegram_bot_bp`
from chatbot.chatbot import chatbot_bp
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
#ALLOWED_ORIGINS = ["https://exadooo-git-main-mohammeds-projects-3d2877c6.vercel.app", "https://exadoo.onrender.com", "https://telegram.org"]
#app = cors(app, allow_origin=ALLOWED_ORIGINS)


ALLOWED_ORIGINS = ["https://exadooo-git-main-mohammeds-projects-3d2877c6.vercel.app", "https://telegram.org", "https://exaadoooo-p.vercel.app", "https://exadoo-rxr9.onrender.com",]
app = cors(app, allow_origin=ALLOWED_ORIGINS, allow_credentials=True)



# 🔹 تسجيل نقاط API
app.register_blueprint(public_routes)
app.register_blueprint(admin_routes)
app.register_blueprint(auth_routes)
app.register_blueprint(subscriptions_bp)
app.register_blueprint(user_bp)
app.register_blueprint(shop)
app.register_blueprint(payment_confirmation_bp)
#app.register_blueprint(webhook_bp)
app.register_blueprint(telegram_bot_bp)  # ✅ تغيير الاسم إلى `telegram_bot_bp`
# تسجيل Blueprint البوت الخاص بالدردشة مع URL prefix
app.register_blueprint(chatbot_bp, url_prefix="/bot")

# بعد إنشاء التطبيق، قبل تسجيل نقاط النهاية الأخرى أو قبل start_serving
@app.after_request
async def add_security_headers(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://accounts.google.com; "
        "frame-src 'self' https://accounts.google.com; "
        "connect-src 'self' https://accounts.google.com https://api.github.com https://api.nepcha.com http://localhost:5000;"
    )
    return response


# 🔹 تشغيل Webhook للبوت عند بدء التطبيق
@app.before_serving
async def create_db_connection():
    try:
        logging.info("🔄 جاري الاتصال بقاعدة البيانات...")
        app.db_pool = await asyncpg.create_pool(**DATABASE_CONFIG)
        app.aiohttp_session = aiohttp.ClientSession()
        logging.info("✅ تم الاتصال بقاعدة البيانات وإنشاء جلسة aiohttp بنجاح.")
        app.bot = bot
        await start_scheduler(app.db_pool)  # ✅ تشغيل الجدولة
        if not getattr(app, "bot_running", False):  # ✅ تأكد من أن البوت لم يتم تشغيله بالفعل
            app.bot_running = True
            asyncio.create_task(start_bot())  # ✅ تشغيل البوت مرة واحدة فقط

        logging.info("✅ جميع الخدمات تم تشغيلها بنجاح.")

    except Exception as e:
        logging.critical(f"🚨 فشل بدء التطبيق: {e}")
        raise RuntimeError("🚨 حدث خطأ أثناء تشغيل التطبيق.") from e

# 🔹 إغلاق الموارد عند إيقاف التطبيق
@app.after_serving
async def close_resources():
    try:
        logging.info("🔄 جاري إغلاق الجلسات المفتوحة...")
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


# 🔹 تشغيل التطبيق باستخدام Hypercorn
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))  # Heroku يحدد المنفذ تلقائيًا
    logging.info(f"🚀 تشغيل Exadoo API على المنفذ {port}...")

    config = hypercorn.Config()
    config.bind = [f"0.0.0.0:{port}"]  # استخدام المنفذ الصحيح

    asyncio.run(hypercorn.asyncio.serve(app, config))