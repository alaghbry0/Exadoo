# app.py

import asyncpg
import logging
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp
from pgvector.asyncpg import register_vector
from quart import Quart, request  # --- تعديل: إضافة request
from quart_cors import cors

# --- إضافة: استيرادات خاصة بالويب هوك ---
from aiogram.types import Update

# --- تعديل: التأكد من استيراد bot و dp ---
# تأكد من أن ملف telegram_bot.py يحتوي على dp = Dispatcher()
from telegram_bot import bot, dp, telegram_bot_bp

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
from routes.payment_status import payment_status_bp
from routes.payment_confirmation import payment_confirmation_bp
from routes.auth_routes import auth_routes
from services.messaging_service import BackgroundMessagingService
from chatbot.chatbot import chatbot_bp
from chatbot.knowledge_base import knowledge_base
from chatbot.chat_manager import ChatManager
from chatbot.embedding_service import ImprovedEmbeddingService
from chatbot.admin_panel import admin_chatbot_bp
from utils.scheduler import start_scheduler
from utils.db_utils import close_telegram_bot_session

from pytoniq import LiteBalancer

# --- تعديل: إضافة PUBLIC_DOMAIN إلى المتغيرات المطلوبة ---
REQUIRED_ENV_VARS = ["PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "PORT", "PUBLIC_DOMAIN"]
for var in REQUIRED_ENV_VARS:
    if not os.environ.get(var):
        raise ValueError(f"❌ متغير البيئة {var} غير مضبوط.")

# --- إضافة: تعريف ثوابت الويب هوك ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
# يجب أن يكون هذا هو النطاق العام لتطبيقك، e.g., https://your-app.onrender.com
PUBLIC_DOMAIN = os.environ.get("PUBLIC_DOMAIN")

WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_URL = f"{PUBLIC_DOMAIN.rstrip('/')}{WEBHOOK_PATH}"


# دالة تُنفّذ على كل اتصال جديد في pool
async def _on_connect(conn):
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    await register_vector(conn)


# تهيئة التطبيق
app = Quart(__name__)
app.db_pool = None
app.aiohttp_session = None
app.bot = None
app.bot_running = False  # هذا المتغير لم يعد له تأثير كبير لكن يمكن إبقاؤه
app.lite_balancer = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# تسجيل الخدمات والـ Blueprints
app.chat_manager = ChatManager(app)
app.kb = knowledge_base
app = cors(app, allow_origin="*")

app.register_blueprint(notifications_bp, url_prefix="/api")
app.register_blueprint(public_routes)
app.register_blueprint(admin_routes)
app.register_blueprint(permissions_routes)
app.register_blueprint(auth_routes)
app.register_blueprint(payment_status_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(user_bp)
app.register_blueprint(shop)
app.register_blueprint(admin_chatbot_bp)
app.register_blueprint(telegram_bot_bp)
app.register_blueprint(chatbot_bp, url_prefix="/bot")
app.register_blueprint(ws_bp)


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

        logging.info("🔄 Initializing TON LiteBalancer...")
        config_url = 'https://ton.org/global-config.json'

        logging.info(f"Downloading TON config from {config_url}...")
        async with app.aiohttp_session.get(config_url) as response:
            response.raise_for_status()
            ton_config = await response.json()
        logging.info("✅ TON config downloaded successfully.")

        app.lite_balancer = LiteBalancer.from_config(
            config=ton_config, trust_level=2
        )
        await app.lite_balancer.start_up()
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

        logging.info("🔄 Initializing bot and scheduler...")
        app.bot = bot  # تعيين البوت على كائن التطبيق
        logging.info("🔄 Initializing Background Messaging Service...")
        app.messaging_service = BackgroundMessagingService(app.db_pool, app.bot)
        logging.info("✅ Background Messaging Service initialized")
        await start_scheduler(app.bot, app.db_pool)

        # --- ⚠️ حذف: تم إزالة السطر التالي لأنه خاص بالـ polling ---
        # if not app.bot_running:
        #     app.bot_running = True
        #     asyncio.create_task(start_bot())
        logging.info("✅ Bot is configured for Webhook mode. Polling is disabled.")

        app.register_blueprint(payment_confirmation_bp)
        logging.info("✅ Application initialization completed")

    except Exception as e:
        logging.critical(f"🚨 Initialization failed: {e}", exc_info=True)
        await close_resources()
        raise


# إغلاق الموارد عند إيقاف التشغيل
@app.after_serving
async def close_resources():
    try:
        # --- إضافة: حذف الويب هوك عند إيقاف التطبيق ---
        if app.bot:
            logging.info("🔄 Deleting webhook...")
            await app.bot.delete_webhook()
            logging.info("✅ Webhook has been deleted.")

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


# تشغيل التهيئة وإعداد الويب هوك قبل بدء استقبال الطلبات
@app.before_serving
async def setup():
    try:
        await initialize_app()
        logging.info("✅ Final initialization check complete.")

        # --- إضافة: إعداد الويب هوك عند بدء التشغيل ---
        logging.info(f"🔄 Setting up webhook to: {WEBHOOK_URL}")
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url != WEBHOOK_URL:
            await bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET
            )
            logging.info("✅ Webhook has been set successfully.")
        else:
            logging.info("✅ Webhook is already set correctly.")

    except Exception as e:
        logging.critical(f"Initialization or webhook setup failed in setup: {e}")
        raise


# نقطة فحص صحية
@app.route("/")
async def home():
    return "🚀 Exadoo API is running with Telegram Webhook!"


# --- إضافة: نقطة النهاية (endpoint) لاستقبال التحديثات من تيليجرام ---
@app.route(WEBHOOK_PATH, methods=["POST"])
async def bot_webhook():
    # التحقق من الـ secret token للأمان
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "Forbidden", 403

    try:
        # تحويل الطلب إلى كائن Update وتمريره إلى Dispatcher
        update_data = await request.get_json(force=True)
        update_obj = Update.model_validate(update_data, context={"bot": bot}) # غيّرت اسم المتغير لتجنب الارتباك
        
        # ✅  التصحيح الرئيسي هنا
        await dp.feed_update(update=update_obj) 
        
        return "", 200
    except Exception as e:
        logging.error(f"Error processing webhook: {e}", exc_info=True)
        return "Internal Server Error", 500


# نقطة الدخول الرئيسية
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    config = hypercorn.Config()
    config.bind = [f"0.0.0.0:{port}"]
    config.worker_class = 'asyncio'
    config.startup_timeout = 90.0
    asyncio.run(hypercorn.asyncio.serve(app, config))
