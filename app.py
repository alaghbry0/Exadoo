# app.py

import logging
import asyncpg
import os
import asyncio
import hypercorn.config
import hypercorn.asyncio
import aiohttp
from pgvector.asyncpg import register_vector
from quart import Quart
from quart_cors import cors
from config import DATABASE_CONFIG
from routes.users import user_bp
from routes.admin_routes import admin_routes
from routes.permissions_routes import permissions_routes
from routes.notifications_routes import notifications_bp
from routes.subscriptions_routs import public_routes
from routes.telegram_payments import payment_bp
from routes.payment_streaming_confirmation import payment_streaming_bp
from routes.payment_status import payment_status_bp
from routes.payment_confirmation import payment_confirmation_bp
from routes.auth_routes import auth_routes
from services.background_task_service import BackgroundTaskService
from services.sse_client import SseApiClient
from telegram_bot import start_bot, bot, telegram_bot_bp
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


# ✅ استخدم Quart العادي، لم نعد بحاجة لـ CustomQuart
app = Quart(__name__)

app.sse_client = None
app.db_pool = None
app.aiohttp_session = None
app.bot = None
app.bot_running = False
app.lite_balancer = None
app.background_task_service = None

# إعداد السجلات (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('LiteClient').setLevel(logging.ERROR)
logging.getLogger('LiteBalancer').setLevel(logging.ERROR)
logging.getLogger('pytoniq').setLevel(logging.ERROR)

# ======================================================================
# ========= 🟢 إعدادات CORS وتسجيل Blueprints (تبقى كما هي) =========
# ======================================================================

# 1. تعريف المصادر الموثوقة للواجهة الأمامية
SECURE_FRONTEND_ORIGINS = [
    "http://localhost:5000",
    "http://localhost:5001",
    "https://exaado-panel.vercel.app"
]

# --- المجموعة الأولى: Blueprints الآمنة (تتطلب كوكيز وبيانات اعتماد) ---
cors(
    auth_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(auth_routes)

cors(
    admin_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(admin_routes)

cors(
    permissions_routes,
    allow_origin=SECURE_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"]
)
app.register_blueprint(permissions_routes)

# --- المجموعة الثانية: Blueprints العامة (متاحة لأي مصدر) ---
cors(notifications_bp, allow_origin="*")
app.register_blueprint(notifications_bp)

cors(public_routes, allow_origin="*")
app.register_blueprint(public_routes)

cors(payment_status_bp, allow_origin="*")
app.register_blueprint(payment_status_bp)

cors(payment_bp, allow_origin="*")
app.register_blueprint(payment_bp)

cors(user_bp, allow_origin="*")
app.register_blueprint(user_bp)

cors(payment_confirmation_bp, allow_origin="*")
app.register_blueprint(payment_confirmation_bp)

# Blueprints لا تحتاج لـ CORS محدد
app.register_blueprint(telegram_bot_bp)



# ======================================================================
# ========= 🔐 إعدادات الأمان (تبقى كما هي) =========
# ======================================================================

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


# ======================================================================
# ========= 🚀 دورة حياة التطبيق الجديدة (Startup & Shutdown) =========
# ======================================================================

@app.before_serving
async def startup():
    """
    يتم استدعاؤه مرة واحدة عند بدء التشغيل.
    هذا هو المكان المثالي لإنشاء الاتصالات وبدء المهام الخلفية.
    """
    logging.info("--- API SERVER: STARTING APPLICATION SETUP ---")

    try:
        # 1. إنشاء الاتصالات الأساسية
        logging.info("API-SERVER: Creating database connection pool...")
        app.db_pool = await asyncpg.create_pool(**DATABASE_CONFIG, init=_on_connect, min_size=5, max_size=50)
        logging.info("API-SERVER: Database pool created.")

        logging.info("API-SERVER: Initializing aiohttp session...")
        app.aiohttp_session = aiohttp.ClientSession()
        logging.info("API-SERVER: aiohttp session initialized.")
        app.sse_client = SseApiClient(app.aiohttp_session)


        # إنشاء LiteBalancer
        config_url = 'https://ton.org/global-config.json'
        async with app.aiohttp_session.get(config_url) as response:
            response.raise_for_status()
            ton_config = await response.json()
        app.lite_balancer = LiteBalancer.from_config(config=ton_config, trust_level=2)
        await app.lite_balancer.start_up()
        logging.info("API-SERVER: TON LiteBalancer initialized and connected.")

        # إنشاء background_task_service
        app.bot = bot
        app.background_task_service = BackgroundTaskService(app.db_pool, app.bot)
        logging.info("API-SERVER: Background Task Service initialized.")

        # 2. الآن بعد أن أصبحت كل الكائنات جاهزة، ابدأ المهام الخلفية
        await mark_stale_tasks_as_failed(app.db_pool)

        logging.info("API-SERVER: Starting Telegram bot and scheduler...")
        await start_scheduler(app.bot, app.db_pool)
        if not app.bot_running:
            app.bot_running = True
            asyncio.create_task(start_bot())
        app.register_blueprint(payment_streaming_bp)
        logging.info("✅ Application initialization completed")

        logging.info("--- API SERVER: APPLICATION SETUP COMPLETE ---")

    except Exception as e:
        logging.critical(f"🚨 APPLICATION STARTUP FAILED: {e}", exc_info=True)
        # قم بإغلاق الموارد التي تم فتحها قبل حدوث الخطأ
        await shutdown()
        raise


@app.after_serving
async def shutdown():
    """
    يتم استدعاؤه مرة واحدة عند الإيقاف.
    يقوم بإغلاق كل الاتصالات المفتوحة.
    """
    logging.info("--- API SERVER: STARTING APPLICATION SHUTDOWN ---")
    if app.aiohttp_session and not app.aiohttp_session.closed:
        await app.aiohttp_session.close()
        logging.info("API-SERVER: aiohttp session closed")
    if app.db_pool:
        await app.db_pool.close()
        logging.info("API-SERVER: Database pool closed")
    if app.lite_balancer:
        await app.lite_balancer.close_all()
        logging.info("API-SERVER: TON LiteBalancer connections closed")
    if app.bot:
        await close_telegram_bot_session(app.bot)
        logging.info("API-SERVER: Telegram bot session closed")
    logging.info("--- API SERVER: APPLICATION SHUTDOWN COMPLETE ---")


# ======================================================================
# ========= ▶️ نقطة تشغيل الخادم (تبقى كما هي) =========
# ======================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    config = hypercorn.config.Config()
    config.bind = [f"0.0.0.0:{port}"]
    config.worker_class = 'asyncio'
    config.keep_alive_timeout = 300.0
    config.h11_max_incomplete_size = 16384

    logging.info(f"🚀 Starting main API server (Quart/Hypercorn) on {config.bind[0]}")
    asyncio.run(hypercorn.asyncio.serve(app, config))