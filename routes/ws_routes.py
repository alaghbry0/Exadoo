# server/ws_routes.py
from quart import Blueprint, websocket, request, jsonify
import asyncio
import json
import logging
import traceback

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}

# دالة للتحقق من صحة telegram_id
async def validate_telegram_id(telegram_id):
    # في بيئة الإنتاج يمكن التحقق من API Telegram أو قاعدة البيانات
    return telegram_id is not None and telegram_id.strip() != '' and telegram_id.isdigit()

@ws_bp.websocket('/ws/notifications/<telegram_id>')
async def notifications_ws(telegram_id):
    # التحقق من صحة telegram_id
    if not await validate_telegram_id(telegram_id):
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"✅ تم فتح اتصال WebSocket لـ telegram_id: {telegram_id}")

    try:
        while True:
            data = await websocket.receive()
            try:
                # تحويل الرسالة المستلمة من JSON إلى كائن Python
                message = json.loads(data)
                logging.info(f"Received message from {telegram_id}: {message}")

                # إرسال تأكيد الاستلام مع إمكانية إضافة منطق إضافي
                await websocket.send(json.dumps({
                    "status": "received",
                    "message": message
                }))
            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "status": "error",
                    "message": "Invalid JSON format"
                }))
    except Exception as e:
        logging.error(f"❌ خطأ في اتصال WebSocket لـ {telegram_id}: {e}")
        logging.error(traceback.format_exc())
    finally:
        if telegram_id in active_connections:
            try:
                active_connections[telegram_id].remove(ws)
                if not active_connections[telegram_id]:
                    del active_connections[telegram_id]
            except ValueError:
                pass
            logging.info(f"🔌 تم إغلاق اتصال WebSocket لـ telegram_id: {telegram_id}")

# وظيفة خدمية لإرسال رسالة إلى مستخدم معين عبر WebSocket
def broadcast_unread_count(telegram_id, unread_count):
    if str(telegram_id) in active_connections:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })
        for ws in active_connections[str(telegram_id)]:
            try:
                asyncio.create_task(ws.send(message))
            except Exception as e:
                logging.error(f"فشل الإرسال لـ {telegram_id}: {e}")
