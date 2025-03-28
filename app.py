import asyncpg
import logging
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from routes.admin_routes import admin_routes
from routes.subscriptions_routs import public_routes
from routes.telegram_payments import payment_bp
from routes.payment_confirmation import payment_confirmation_bp
from routes.auth_routes import auth_routes
from telegram_bot import start_bot, bot, telegram_bot_bp
from chatbot.chatbot import chatbot_bp
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session
from server.sse_routes import sse_bp
from server.redis_manager import redis_manager



# التحقق من المتغيرات البيئية الأساسية
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "PORT"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط.")

# تهيئة التطبيق
app = Quart(__name__)
app.db_pool = None  # Initialize db_pool
app.aiohttp_session = None  # Initialize aiohttp session
app.bot = None  # Initialize bot
app.bot_running = False  # Bot running state

# إعدادات CORS
ALLOWED_ORIGINS = [
    "https://exadooo-plum.vercel.app",
    "https://exaado-panel.vercel.app",
    "https://exadoo-rxr9.onrender.com",
    "https://telegram.org",
    "http://localhost:5000"
]
app = cors(app, allow_origin=ALLOWED_ORIGINS, allow_credentials=True)

# تسجيل Blueprints
app.register_blueprint(sse_bp, url_prefix='/api')

app.register_blueprint(public_routes)
app.register_blueprint(admin_routes)
app.register_blueprint(auth_routes)
app.register_blueprint(subscriptions_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(user_bp)
app.register_blueprint(shop)
app.register_blueprint(telegram_bot_bp)
app.register_blueprint(chatbot_bp, url_prefix="/bot")


# إضافة رؤوس أمان
@app.after_request
async def add_security_headers(response):
    headers = {
        'Cross-Origin-Opener-Policy': 'same-origin-allow-popups',
        'Content-Security-Policy': (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://accounts.google.com; "
            "frame-src 'self' https://accounts.google.com; "
            "connect-src 'self' https://accounts.google.com https://api.github.com https://api.nepcha.com http://localhost:5000; https://exaado-panel.vercel.app"
        )
    }
    response.headers.update(headers)
    return response


# تهيئة التطبيق والاتصالات
async def initialize_app():
    """Initialize application connections"""
    try:
        logging.info(f"🔄 محاولة الاتصال بـ {DATABASE_CONFIG['host']}:{DATABASE_CONFIG['port']}")
        # Initialize Redis
        logging.info("🔄 Connecting to Redis...")
        await redis_manager.connect()
        logging.info("✅ Redis connection established")


        # Initialize PostgreSQL connection pool
        logging.info("🔄 Creating database connection pool...")
        app.db_pool = await asyncpg.create_pool(**DATABASE_CONFIG)
        logging.info("✅ Database pool created")

        # Initialize aiohttp session
        logging.info("🔄 Initializing aiohttp session...")
        app.aiohttp_session = aiohttp.ClientSession()
        logging.info("✅ aiohttp session initialized")

        # Start Telegram bot and scheduler
        logging.info("🔄 Starting Telegram bot...")
        app.bot = bot
        await start_scheduler(app.db_pool)
        if not app.bot_running:
            app.bot_running = True
            asyncio.create_task(start_bot())

        app.register_blueprint(payment_confirmation_bp)

        logging.info("✅ Application initialization completed")

    except Exception as e:
        logging.critical(f"🚨 Initialization failed: {str(e)}", exc_info=True)
        await close_resources()
        raise


# إغلاق الموارد
@app.after_serving
async def close_resources():
    """Cleanup resources on shutdown"""
    try:
        logging.info("🔄 Cleaning up resources...")

        # Close Redis connection
        if hasattr(redis_manager, 'close'):
            await redis_manager.close()
            logging.info("✅ Redis connection closed")

        # Close aiohttp session
        if app.aiohttp_session and not app.aiohttp_session.closed:
            await app.aiohttp_session.close()
            logging.info("✅ aiohttp session closed")

        # Close database pool
        if app.db_pool:
            await app.db_pool.close()
            logging.info("✅ Database pool closed")

        # Close Telegram bot session
        await close_telegram_bot_session()

    except Exception as e:
        logging.error(f"❌ Error during cleanup: {str(e)}")


# تهيئة قبل التشغيل
@app.before_serving
async def setup():
    await initialize_app()


# Route for health check
@app.route("/")
async def home():
    return "🚀 Exadoo API is running!"


# تشغيل التطبيق
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    config = hypercorn.Config()
    config.bind = [f"0.0.0.0:{port}"]
    config.startup_timeout = 60.0  # Increase startup timeout
    asyncio.run(hypercorn.asyncio.serve(app, config))