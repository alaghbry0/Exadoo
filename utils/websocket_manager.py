import json
import logging
from typing import Dict, Any
from quart.websocket import WebSocket


class WebSocketManager:
    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}  # تغيير النوع إلى str

    async def send_to_user(self, telegram_id: int, message: dict):
        # تحويل الـ telegram_id إلى سلسلة لتتناسب مع مفتاح القاموس
        str_id = str(telegram_id)
        connection = self.connections.get(str_id)

        if not connection:
            logging.warning(f"⚠️ لا يوجد اتصال نشط للمستخدم {telegram_id}")
            return

        try:
            if not connection.closed:
                await connection.send(json.dumps(message))
                logging.info(f"✅ تم إرسال إشعار إلى المستخدم {telegram_id}")
            else:
                logging.warning(f"⚠️ اتصال WebSocket مغلق للمستخدم {telegram_id}")
                del self.connections[str_id]
        except Exception as e:
            logging.error(f"❌ فشل الإرسال للمستخدم {telegram_id}: {e}")
            del self.connections[str_id]


def init_websocket_manager(app):
    app.ws_manager = WebSocketManager()
    logging.info("🚀 تم تهيئة مدير WebSocket")