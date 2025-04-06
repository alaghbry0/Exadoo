# server/ws_routes.py
from quart import Blueprint, websocket
import asyncio
import json
import logging
from weakref import WeakKeyDictionary, WeakValueDictionary

ws_bp = Blueprint('ws_bp', __name__)

active_connections = WeakValueDictionary()  # استخدام WeakValueDictionary للإدارة التلقائية
connection_users = WeakKeyDictionary()  # تتبع المستخدم لكل اتصال


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not telegram_id or not telegram_id.isdigit():
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()
    connection_users[ws] = telegram_id

    # إضافة الاتصال إلى القائمة
    if telegram_id not in active_connections:
        active_connections[telegram_id] = set()
    active_connections[telegram_id].add(ws)

    logging.info(f"✅ New WebSocket connection for {telegram_id} (Total: {len(active_connections.get(telegram_id, []))}")

    try:
        while True:
            # إرسال ping بشكل دوري للحفاظ على الاتصال
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception as e:
                logging.error(f"Ping failed for {telegram_id}: {e}")
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"WebSocket error for {telegram_id}: {e}")
    finally:
        if telegram_id in active_connections:
            active_connections[telegram_id].discard(ws)
            if not active_connections[telegram_id]:
                del active_connections[telegram_id]
        logging.info(f"🔌 Disconnected WebSocket for {telegram_id}")


def broadcast_unread_count(telegram_id: str, unread_count: int):
    connections = active_connections.get(str(telegram_id), set())
    message = json.dumps({
        "type": "unread_update",
        "data": {"count": unread_count}
    })

    for ws in list(connections):  # نسخ القائمة لتجنب التعديل أثناء التكرار
        try:
            asyncio.create_task(ws.send(message))
        except Exception as e:
            logging.error(f"Failed to send to {telegram_id}: {e}")
            connections.discard(ws)