# app.py

import asyncpg
import logging
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp
from pgvector.asyncpg import register_vector
from quart import Quart
from quart_cors import cors
from chatbot.ai_service import DeepSeekService
from config import DATABASE_CONFIG
from routes.users import user_bp
from routes.shop import shop
from routes.admin_routes import admin_routes
from routes.permissions_routes import permissions_routes
from routes.notifications_routes import notifications_bp
from routes.subscriptions_routs import public_routes
from routes.telegram_payments import payment_bp
from routes.ws_routes import ws_bp
from routes.payment_streaming_confirmation import payment_streaming_bp
from routes.payment_status import payment_status_bp
from routes.payment_confirmation import payment_confirmation_bp
from routes.auth_routes import auth_routes
from services.background_task_service import BackgroundTaskService
from telegram_bot import start_bot, bot, telegram_bot_bp
from chatbot.chatbot import chatbot_bp
from chatbot.knowledge_base import knowledge_base
from chatbot.chat_manager import ChatManager
from chatbot.embedding_service import ImprovedEmbeddingService
from chatbot.admin_panel import admin_chatbot_bp
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session
from utils.startup_tasks import mark_stale_tasks_as_failed
from pytoniq import LiteBalancer

# تأكد من المتغيرات البيئية الأساسية
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "PORT"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط.")


# دالة تُنفّذ على كل اتصال جديد في pool
async def _on_connect(conn):
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    await register_vector(conn)


# تهيئة التطبيق
app = Quart(__name__)
app.db_pool = None
app.aiohttp_session = None
app.bot = None
app.bot_running = False
app.lite_balancer = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# تسجيل الخدمات والـ Blueprints
app.chat_manager = ChatManager(app)
app.kb = knowledge_base
# ======================================================================
# ========= 🟢 بداية التعديلات: الطريقة الصحيحة لتطبيق CORS على Blueprints =========
# ======================================================================

# 1. تعريف المصادر الموثوقة للواجهة الأمامية
SECURE_FRONTEND_ORIGINS = [
    "http://localhost:5001",
    "https://exaado-panel.vercel.app"
]

# --- المجموعة الأولى: Blueprints الآمنة (تتطلب كوكيز وبيانات اعتماد) ---
# قم بتطبيق سياسة CORS الصارمة على كل blueprint آمن على حدة
# 💡 التعديل الرئيسي: إضافة allow_methods و allow_headers بشكل صريح
cors(
    auth_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],  # اسمح بالوسائل التي تستخدمها هذه المسارات
    allow_headers=["Content-Type", "Authorization"] # اسمح بالهيدرات التي ترسلها الواجهة الأمامية
)
app.register_blueprint(auth_routes)

cors(
    admin_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], # كن أكثر كرماً هنا
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(admin_routes)

cors(
    permissions_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(permissions_routes)

cors(
    admin_chatbot_bp,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(admin_chatbot_bp)
# أضف أي blueprint آخر يتطلب تسجيل دخول هنا بنفس الطريقة



# --- المجموعة الثانية: Blueprints العامة (متاحة لأي مصدر) ---
# قم بتطبيق سياسة CORS العامة على كل blueprint عام
cors(notifications_bp, allow_origin="*")
app.register_blueprint(notifications_bp, url_prefix="/api")

cors(public_routes, allow_origin="*")
app.register_blueprint(public_routes)

cors(payment_status_bp, allow_origin="*")
app.register_blueprint(payment_status_bp)

cors(payment_bp, allow_origin="*")
app.register_blueprint(payment_bp)

cors(user_bp, allow_origin="*")
app.register_blueprint(user_bp)

cors(shop, allow_origin="*")
app.register_blueprint(shop)

cors(chatbot_bp, allow_origin="*")
app.register_blueprint(chatbot_bp, url_prefix="/bot")

cors(payment_confirmation_bp, allow_origin="*")
app.register_blueprint(payment_confirmation_bp)

cors(ws_bp, allow_origin="*")
app.register_blueprint(ws_bp)
app.register_blueprint(telegram_bot_bp)


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
            **DATABASE_CONFIG, init=_on_connect, min_size=5, max_size=50
        )
        logging.info("✅ Database pool created with pgvector support")

        logging.info("🔄 Initializing aiohttp session...")
        app.aiohttp_session = aiohttp.ClientSession()
        logging.info("✅ aiohttp session initialized")

        await mark_stale_tasks_as_failed(app.db_pool)

        logging.info("🔄 Initializing TON LiteBalancer...")
        config_url = 'https://ton.org/global-config.json'

        logging.info(f"Downloading TON config from {config_url}...")
        async with app.aiohttp_session.get(config_url) as response:
            response.raise_for_status()
            ton_config = await response.json()
        logging.info("✅ TON config downloaded successfully.")

        # --- 🟢 تصحيح: إزالة 'await' من هنا لأن from_config دالة متزامنة ---
        app.lite_balancer = LiteBalancer.from_config(
            config=ton_config, trust_level=2
        )
        # --- نهاية التصحيح ---

        await app.lite_balancer.start_up()  # الـ await هنا صحيح لأن start_up غير متزامنة
        logging.info("✅ TON LiteBalancer initialized and connected.")

        logging.info("🔄 Initializing AI service...")
        app.ai_service = DeepSeekService(app)
        logging.info("✅ AI service initialized")

        logging.info("🔄 Initializing Embedding service...")
        app.embedding_service = ImprovedEmbeddingService()
        await app.embedding_service.initialize()
        logging.info("✅ Embedding service initialized")

        knowledge_base.init_app(app)
        logging.info("✅ KnowledgeBase initialized")
        app.chat_manager.init_app(app)

        logging.info("🔄 Starting Telegram bot and scheduler...")
        app.bot = bot
        logging.info("🔄 Initializing Background Messaging Service...")
        app.background_task_service = BackgroundTaskService(app.db_pool, app.bot)
        logging.info("✅ Background Task Service initialized")
        await start_scheduler(app.bot, app.db_pool)
        if not app.bot_running:
            app.bot_running = True
            asyncio.create_task(start_bot())

        app.register_blueprint(payment_streaming_bp)
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
        if app.lite_balancer:
            await app.lite_balancer.close_all()
            logging.info("✅ TON LiteBalancer connections closed")
        if app.bot:
            await close_telegram_bot_session(app.bot)
    except Exception as e:
        logging.error(f"❌ Error during cleanup: {e}")


# تشغيل التهيئة قبل البدء في استقبال الطلبات
@app.before_serving
async def setup():
    try:
        await initialize_app()
        logging.info("✅ Final initialization check complete.")
    except Exception as e:
        logging.critical(f"Initialization failed in setup: {e}")
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
    config.startup_timeout = 90.0
    asyncio.run(hypercorn.asyncio.serve(app, config))
