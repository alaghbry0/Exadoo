import json
import logging
from typing import Dict, Any
from quart import websocket  # التعديل هنا


class WebSocketManager:
    def __init__(self):
        self.connections: Dict[str, websocket.WebSocket] = {}  # استخدام websocket.WebSocket

    async def send_to_user(self, telegram_id: int, message: dict):
        str_id = str(telegram_id)
        if str_id not in self.connections:
            logging.warning(f"⚠️ لا يوجد اتصال نشط للمستخدم {telegram_id}")
            return

        connection = self.connections[str_id]

        try:
            if connection.closed:
                logging.warning(f"اتصال مغلق - إزالته من السجلات")
                del self.connections[str_id]
                return

            await connection.send(json.dumps(message))
            logging.info(f"✅ إشعار مرسل بنجاح لـ {telegram_id}")

        except Exception as e:
            logging.error(f"❌ فشل الإرسال: {str(e)}")
            del self.connections[str_id]


def init_websocket_manager(app):
    app.ws_manager = WebSocketManager()
    logging.info("🚀 تم تهيئة مدير WebSocket")