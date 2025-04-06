# shared_state.py
from typing import Dict, List
from quart import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, telegram_id: str, websocket: WebSocket):
        if telegram_id not in self.active_connections:
            self.active_connections[telegram_id] = []
        self.active_connections[telegram_id].append(websocket)

    def disconnect(self, telegram_id: str, websocket: WebSocket):
        if telegram_id in self.active_connections:
            self.active_connections[telegram_id].remove(websocket)
            if not self.active_connections[telegram_id]:
                del self.active_connections[telegram_id]

    def get_connections(self, telegram_id: str) -> List[WebSocket]:
        return self.active_connections.get(telegram_id, [])

connection_manager = ConnectionManager()