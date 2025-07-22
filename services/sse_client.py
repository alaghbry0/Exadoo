# services/sse_client.py

import os
import logging
import aiohttp
import json

SSE_INTERNAL_URL = os.environ.get("SSE_INTERNAL_URL")
INTERNAL_SECRET_KEY = os.environ.get("INTERNAL_SECRET_KEY")


class SseApiClient:
    """
    هذه الفئة هي "العميل".
    يستخدمها خادم API لإرسال طلبات النشر إلى خدمة SSE.
    """

    def __init__(self, session: aiohttp.ClientSession):
        if not all([SSE_INTERNAL_URL, INTERNAL_SECRET_KEY]):
            raise ValueError("SSE_INTERNAL_URL or INTERNAL_SECRET_KEY is not set in environment!")

        self.session = session
        self.url = SSE_INTERNAL_URL
        self.headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": INTERNAL_SECRET_KEY
        }
        logging.info("✅ SSE API Client initialized.")

    async def publish(self, telegram_id: str, message_type: str, data: dict):
        payload = {
            "telegram_id": str(telegram_id),
            "message_type": message_type,
            "payload": data
        }
        try:
            async with self.session.post(self.url, json=payload, headers=self.headers, timeout=5) as response:
                if response.status != 200:
                    logging.error(f"Failed to publish SSE event. Status: {response.status}")
        except Exception as e:
            logging.error(f"Error publishing SSE event: {e}")