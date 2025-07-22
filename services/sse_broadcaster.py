# services/sse_broadcaster.py

import asyncio
import json
import logging

class SSEBroadcaster:
    """
    هذه الفئة هي "الخادم".
    تدير اتصالات SSE النشطة والطوابير داخل خدمة SSE.
    """
    def __init__(self):
        self._connections = {}
        logging.info("✅ SSE Broadcaster (Server Mode) initialized.")

    async def publish(self, telegram_id: str, message_type: str, data: dict):
        telegram_id_str = str(telegram_id)
        if telegram_id_str not in self._connections:
            return

        formatted_message = f"event: {message_type}\ndata: {json.dumps(data)}\n\n"
        for queue_info in self._connections[telegram_id_str]:
            try:
                queue_info['queue'].put_nowait(formatted_message)
            except asyncio.QueueFull:
                logging.warning(f"SSE-SERVER: Queue full for user {telegram_id_str}, message dropped.")

    def subscribe(self, telegram_id: str) -> dict:
        telegram_id_str = str(telegram_id)
        queue = asyncio.Queue(maxsize=100)
        connection_info = {'queue': queue}
        if telegram_id_str not in self._connections:
            self._connections[telegram_id_str] = []
        self._connections[telegram_id_str].append(connection_info)
        logging.info(f"SSE-SERVER: New subscription for {telegram_id_str}.")
        return connection_info

    def unsubscribe(self, telegram_id: str, connection_info: dict):
        telegram_id_str = str(telegram_id)
        if telegram_id_str in self._connections:
            try:
                self._connections[telegram_id_str].remove(connection_info)
                if not self._connections[telegram_id_str]:
                    del self._connections[telegram_id_str]
                logging.info(f"SSE-SERVER: Client unsubscribed for user {telegram_id_str}.")
            except ValueError:
                pass