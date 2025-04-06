# shared_state.py
from typing import Dict, List
from quart import websocket  # تغيير هنا

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[websocket.WebSocket]] = {}  # تعديل هنا

    async def connect(self, telegram_id: str, ws: websocket.WebSocket):  # تعديل هنا
        if telegram_id not in self.active_connections:
            self.active_connections[telegram_id] = []
        self.active_connections[telegram_id].append(ws)

    def disconnect(self, telegram_id: str, ws: websocket.WebSocket):  # تعديل هنا
        if telegram_id in self.active_connections:
            self.active_connections[telegram_id].remove(ws)
            if not self.active_connections[telegram_id]:
                del self.active_connections[telegram_id]

    def get_connections(self, telegram_id: str) -> List[websocket.WebSocket]:  # تعديل هنا
        return self.active_connections.get(telegram_id, [])

connection_manager = ConnectionManager()