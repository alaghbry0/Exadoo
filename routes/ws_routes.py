# server/ws_routes.py
from quart import Blueprint, websocket, request
import asyncio
import json
import logging

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}

@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    # الحصول على telegram_id من معلمات الاستعلام
    telegram_id = request.args.get('telegram_id')
    if not telegram_id:
        await websocket.close(code=4000)
        return

    # إضافة الاتصال إلى القائمة الخاصة بـ telegram_id
    ws = websocket
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"تم فتح اتصال WebSocket لـ telegram_id: {telegram_id}")

    try:
        # حلقة الاستماع؛ يمكن تعديلها لاستقبال رسائل من العميل إذا احتجت لذلك
        while True:
            _ = await websocket.receive()
            await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"خطأ في اتصال WebSocket: {e}")
    finally:
        # إزالة الاتصال عند قطع الاتصال
        active_connections[telegram_id].remove(ws)
        if not active_connections[telegram_id]:
            del active_connections[telegram_id]
        logging.info(f"تم قطع اتصال WebSocket لـ telegram_id: {telegram_id}")

def broadcast_unread_count(telegram_id, unread_count):
    """
    ترسل رسالة تحديث للعميل الذي يحمل telegram_id معين تحتوي على عدد الرسائل غير المقروءة.
    """
    if telegram_id in active_connections:
        message = json.dumps({"unread_count": unread_count})
        for ws in active_connections[telegram_id]:
            asyncio.create_task(ws.send(message))
