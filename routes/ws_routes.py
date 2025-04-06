# server/ws_routes.py
from quart import Blueprint, websocket
import asyncio
import json
import logging
from collections import defaultdict
from weakref import WeakKeyDictionary

ws_bp = Blueprint('ws_bp', __name__)

# استخدام defaultdict لتجنب KeyError
active_connections = defaultdict(set)
connection_lock = asyncio.Lock()
connection_users = WeakKeyDictionary()


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not telegram_id or not telegram_id.isdigit():
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    # استخدام Lock لمنع race conditions
    async with connection_lock:
        active_connections[telegram_id].add(ws)
    connection_users[ws] = telegram_id

    logging.info(f"✅ New WebSocket connection for {telegram_id} (Total: {len(active_connections[telegram_id])}")

    try:
        while True:
            # إرسال ping كل 25 ثانية
            await asyncio.sleep(25)
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
        async with connection_lock:
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

    for ws in list(connections):
        try:
            asyncio.create_task(ws.send(message))
        except Exception as e:
            logging.error(f"Failed to send to {telegram_id}: {e}")
            connections.discard(ws)