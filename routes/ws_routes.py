# server/ws_routes.py
from quart import Blueprint, websocket, request
import asyncio
import json
import logging
import traceback

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}

@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    # ✅ استخدم websocket بدلًا من request
    telegram_id = websocket.request.args.get('telegram_id')
    if not telegram_id or not telegram_id.isdigit():
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"✅ تم فتح اتصال WebSocket لـ telegram_id: {telegram_id}")

    try:
        while True:
            _ = await websocket.receive()
            await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"❌ خطأ في اتصال WebSocket: {e}")
        logging.error(traceback.format_exc())
    finally:
        if telegram_id in active_connections:
            try:
                active_connections[telegram_id].remove(ws)
                if not active_connections[telegram_id]:
                    del active_connections[telegram_id]
            except ValueError:
                pass
            logging.info(f"🔌 تم قطع اتصال WebSocket لـ telegram_id: {telegram_id}")

def broadcast_unread_count(telegram_id, unread_count):
    if telegram_id in active_connections:
        message = json.dumps({"unread_count": unread_count})
        for ws in active_connections[telegram_id]:
            try:
                asyncio.create_task(ws.send(message))
            except Exception as e:
                logging.error(f"فشل إرسال الرسالة: {e}")
