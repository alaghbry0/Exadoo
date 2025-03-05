import json
import logging
from typing import Dict, Any

class WebSocketManager:
    def __init__(self):
        self.connections: Dict[int, Any] = {}

    async def send_to_user(self, telegram_id: int, message: dict):
        if telegram_id in self.connections:
            try:
                await self.connections[telegram_id].send(json.dumps(message))
                logging.info(f"✅ تم إرسال إشعار إلى المستخدم {telegram_id}")
            except Exception as e:
                logging.error(f"❌ فشل الإرسال للمستخدم {telegram_id}: {e}")
                del self.connections[telegram_id]

def init_websocket_manager(app):
    app.ws_manager = WebSocketManager()
    logging.info("🚀 تم تهيئة مدير WebSocket")