from quart import Blueprint, websocket
from server.shared_state import connection_manager
import asyncio
import json
import logging

ws_bp = Blueprint('ws_bp', __name__)

@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    ws = None
    telegram_id = None
    try:
        telegram_id = websocket.args.get('telegram_id')
        if not telegram_id or not telegram_id.isdigit():
            await websocket.close(code=4000)
            return

        ws = websocket._get_current_object()
        await connection_manager.connect(telegram_id, ws)

        while True:
            message = await websocket.receive()
            if message == 'pong':
                continue
            # يمكن هنا إضافة منطق للتعامل مع الرسائل الأخرى

    except asyncio.CancelledError:
        logging.info("تم إلغاء اتصال WebSocket")
    except Exception as e:
        logging.error(f"خطأ في WebSocket: {str(e)}")
    finally:
        if ws is not None and telegram_id is not None:
            connection_manager.disconnect(telegram_id, ws)

def broadcast_unread_count(telegram_id: str, unread_count: int):
    if not isinstance(unread_count, int) or unread_count < 0:
        logging.error(f"عدد الرسائل غير الصحيح: {unread_count}")
        return

    message = json.dumps({
        "type": "unread_update",
        "data": {"count": unread_count}
    })

    for ws in connection_manager.get_connections(str(telegram_id)):
        try:
            asyncio.create_task(ws.send(message))
        except Exception as e:
            logging.error(f"فشل الإرسال لـ {telegram_id}: {str(e)}")
