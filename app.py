# app.py

import asyncpg
import logging
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp
from pgvector.asyncpg import register_vector
from quart import Quart, request
from chatbot.ai_service import DeepSeekService
from config import DATABASE_CONFIG
from routes.subscriptions import subscriptions_bp
from routes.users import user_bp
from routes.shop import shop
from routes.admin_routes import admin_routes
from routes.permissions_routes import permissions_routes
from routes.notifications_routes import notifications_bp
from routes.subscriptions_routs import public_routes
from routes.telegram_payments import payment_bp
from routes.ws_routes import ws_bp
from routes.payment_status import payment_status_bp
from routes.payment_confirmation import payment_confirmation_bp
from routes.auth_routes import auth_routes
from telegram_bot import start_bot, bot, telegram_bot_bp
from chatbot.chatbot import chatbot_bp
from chatbot.knowledge_base import knowledge_base
from chatbot.chat_manager import ChatManager
from chatbot.embedding_service import ImprovedEmbeddingService
from chatbot.admin_panel import admin_chatbot_bp
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session

# تأكد من المتغيرات البيئية الأساسية
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "PORT"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط.")


# دالة تُنفّذ على كل اتصال جديد في pool
async def _on_connect(conn):
    # تأكد من وجود امتداد vector
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    # سجّل codec للـ vector type
    await register_vector(conn)


# تهيئة التطبيق
app = Quart(__name__)
app.db_pool = None
app.aiohttp_session = None
app.bot = None
app.bot_running = False

# إعدادات الجلسة (إذا كنت تستخدمها)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # True إذا كنت تستخدم HTTPS

app.chat_manager = ChatManager(app)
app.kb = knowledge_base

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


# ========== إعدادات CORS المخصصة ==========
@app.after_request
async def handle_cors(response):
    origin = request.headers.get('Origin', '*')

    # السماح لجميع المصادر مع التحكم في Credentials
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Requested-With'
    response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Max-Age'] = '86400'  # 24 ساعة

    return response


# معالجة طلبات OPTIONS لكل المسارات
@app.route("/<path:path>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
async def options_handler(path=None):
    return "", 204


# ========== نهاية إعدادات CORS ==========

# تسجيل Blueprints
app.register_blueprint(notifications_bp, url_prefix="/api")
app.register_blueprint(public_routes)
app.register_blueprint(admin_routes)
app.register_blueprint(permissions_routes)
app.register_blueprint(auth_routes)
app.register_blueprint(payment_status_bp)
app.register_blueprint(subscriptions_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(user_bp)
app.register_blueprint(shop)
app.register_blueprint(admin_chatbot_bp)
app.register_blueprint(telegram_bot_bp)
app.register_blueprint(chatbot_bp, url_prefix="/bot")
app.register_blueprint(ws_bp)


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
    try:
        logging.info("🔄 Creating database connection pool...")
        app.db_pool = await asyncpg.create_pool(
            **DATABASE_CONFIG,
            init=_on_connect,
            min_size=5,
            max_size=50,
        )
        logging.info("✅ Database pool created with pgvector support")

        logging.info("🔄 Initializing aiohttp session...")
        app.aiohttp_session = aiohttp.ClientSession()
        logging.info("✅ aiohttp session initialized")

        logging.info("🔄 Initializing AI service...")
        app.ai_service = DeepSeekService(app)
        logging.info("✅ AI service initialized")

        # 1. تهيئة الخدمة التضمينية أولاً
        logging.info("🔄 Initializing Embedding service...")
        app.embedding_service = ImprovedEmbeddingService()
        await app.embedding_service.initialize()
        logging.info("✅ Embedding service initialized")

        # 2. تهيئة KnowledgeBase بعد وجود embedding_service
        knowledge_base.init_app(app)
        logging.info("✅ KnowledgeBase initialized")

        # 3. تهيئة ChatManager
        app.chat_manager.init_app(app)

        # 3. تهيئة باقي المكونات
        logging.info("🔄 Starting Telegram bot and scheduler...")
        app.bot = bot

        await start_scheduler(app.db_pool)
        if not app.bot_running:
            app.bot_running = True
            asyncio.create_task(start_bot())

        app.register_blueprint(payment_confirmation_bp)
        logging.info("✅ Application initialization completed")

    except Exception as e:
        logging.critical(f"🚨 Initialization failed: {e}", exc_info=True)
        await close_resources()
        raise


# إغلاق الموارد
@app.after_serving
async def close_resources():
    try:
        if app.aiohttp_session and not app.aiohttp_session.closed:
            await app.aiohttp_session.close()
            logging.info("✅ aiohttp session closed")

        if app.db_pool:
            await app.db_pool.close()
            logging.info("✅ Database pool closed")

        await close_telegram_bot_session()
        logging.info("✅ Telegram bot session closed")

    except Exception as e:
        logging.error(f"❌ Error during cleanup: {e}")


# تشغيل التهيئة قبل البدء في استقبال الطلبات
@app.before_serving
async def setup():
    try:
        await initialize_app()
        # التحقق من التهيئة النهائية
        logging.info("Final initialization check:")
        logging.info(f"AI Service initialized: {hasattr(app, 'ai_service')}")
        logging.info(f"Embedding Service: {hasattr(app, 'embedding_service')}")
        logging.info(f"KnowledgeBase AI ref: {knowledge_base.ai_service is not None}")
        logging.info(f"ChatManager initialized: {app.chat_manager is not None}")
    except Exception as e:
        logging.critical(f"Initialization failed: {e}")
        raise


# نقطة فحص صحية
@app.route("/")
async def home():
    return "🚀 Exadoo API is running!"


# نقطة الدخول الرئيسية
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    config = hypercorn.Config()
    config.bind = [f"0.0.0.0:{port}"]
    config.worker_class = 'asyncio'
    config.startup_timeout = 60.0
    asyncio.run(hypercorn.asyncio.serve(app, config))