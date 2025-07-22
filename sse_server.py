# sse_server.py

import os
import asyncio
import logging
import json
from aiohttp import web
from dotenv import load_dotenv # ✅ استيراد جديد
from services.sse_broadcaster import SSEBroadcaster # ✅ استخدم الـ Broadcaster الأصلي

load_dotenv() # ✅ قم بتحميل المتغيرات من ملف .env

# إعداد السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ✅ الآن يجب أن يعمل هذا التحقق بشكل صحيح
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET_KEY")
if not INTERNAL_SECRET:
    raise ValueError("INTERNAL_SECRET_KEY is not set!")

# ✅ هذا صحيح الآن، لأن SSEBroadcaster الأصلي لا يحتاج إلى session
broadcaster = SSEBroadcaster()


# --- معالجات الطلبات (Request Handlers) ---

async def sse_handler(request: web.Request):
    """معالج لاتصالات SSE الواردة من العملاء (المتصفحات)."""
    # ... الكود الحالي لـ sse_handler ممتاز ولا يحتاج لتغيير ...
    # فقط تأكد من أنه يستخدم الـ broadcaster الذي أنشأناه أعلاه.
    telegram_id = request.query.get("telegram_id")
    if not telegram_id or not telegram_id.isdigit():
        return web.Response(text="telegram_id is required and must be a digit.", status=400)

    logging.info(f"SSE-SERVER: Client connected for user {telegram_id}.")
    connection_info = broadcaster.subscribe(telegram_id)
    queue = connection_info['queue']

    response = web.StreamResponse(
        status=200, reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    try:
        await response.write(b"event: connection_established\ndata: {\"status\": \"connected\"}\n\n")
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=15.0)  # استخدام فاصل 15 ثانية
                await response.write(data.encode('utf-8'))
            except asyncio.TimeoutError:
                await response.write(b"event: heartbeat\ndata: \n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        logging.info(f"SSE-SERVER: Client for user {telegram_id} disconnected.")
    finally:
        broadcaster.unsubscribe(telegram_id, connection_info)
    return response


async def internal_publish_handler(request: web.Request):
    """معالج للطلبات الداخلية من خادم API لنشر الإشعارات."""
    # 1. التحقق من الهيدر السري
    if request.headers.get("X-Internal-Secret") != INTERNAL_SECRET:
        logging.warning("SSE-SERVER: Received unauthorized internal publish request.")
        return web.Response(status=403)  # 403 Forbidden

    # 2. استخراج البيانات من الطلب
    try:
        data = await request.json()
        telegram_id = data.get("telegram_id")
        message_type = data.get("message_type")
        payload = data.get("payload")

        if not all([telegram_id, message_type, payload]):
            return web.json_response({"error": "Missing required fields"}, status=400)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # 3. النشر باستخدام الـ broadcaster
    logging.info(f"SSE-SERVER: Received internal publish for user {telegram_id}, type: {message_type}")
    await broadcaster.publish(str(telegram_id), message_type, payload)

    return web.json_response({"status": "published"}, status=200)

# ====================================================================
# ✅ تعديل 1: إضافة معالج لفحص الحالة الصحية (Health Check)
# هذا المعالج سيستجيب لطلبات Render على المسار الرئيسي "/"
# ====================================================================
async def health_check_handler(request: web.Request):
    """
    معالج بسيط يعيد استجابة 200 OK لإعلام منصة النشر
    بأن الخدمة تعمل بشكل صحيح.
    """
    logging.info("Health check endpoint was hit.")
    return web.json_response(
        {"status": "ok", "message": "SSE server is healthy."},
        status=200
    )


# --- نقطة الدخول لتشغيل الخادم ---
if __name__ == "__main__":
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/notifications/stream", sse_handler)
    app.router.add_post("/_internal/publish", internal_publish_handler)

    port = int(os.environ.get("PORT2", 5002))
    logging.info(f"🚀 Starting standalone SSE server on port {port}...")
    web.run_app(app, port=port)