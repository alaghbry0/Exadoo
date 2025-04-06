# server/ws_routes.py
from quart import Blueprint, websocket
from quart.websockets import WebSocket
from server.shared_state import connection_manager
import asyncio
import json
import logging

ws_bp = Blueprint('ws_bp', __name__)


@ws_bp.websocket('/ws/notifications')
async def websocket_handler():
    telegram_id = websocket.args.get('telegram_id')
    if not (telegram_id and telegram_id.isdigit()):
        await websocket.close(code=4000)
        return

    ws: WebSocket = websocket._get_current_object()
    await connection_manager.connect(telegram_id, ws)

    try:
        while True:
            data = await websocket.receive()
            if data == 'pong':
                continue


    except (asyncio.CancelledError, ConnectionResetError):

        logging.info("WebSocket connection closed normally")

    except Exception as e:

        logging.error(f"WebSocket error: {str(e)}")

    finally:

        connection_manager.disconnect(telegram_id, ws)

def broadcast_unread_count(telegram_id: str, unread_count: int):
    if not isinstance(unread_count, int) or unread_count < 0:
        logging.error(f"Invalid unread_count: {unread_count}")
        return

    message = json.dumps({
        "type": "unread_update",
        "data": {"count": unread_count}
    })

    for ws in connection_manager.get_connections(str(telegram_id)):
        try:
            asyncio.create_task(ws.send(message))
        except Exception as e:
            logging.error(f"Failed to send to {telegram_id}: {str(e)}")