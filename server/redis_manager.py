# server/redis_manager.py
import redis.asyncio as redis
import os
import json
import logging
from typing import Optional


class RedisManager:
    _instance: Optional['RedisManager'] = None

    def __init__(self):
        if RedisManager._instance is not None:
            raise RuntimeError("Use get_instance() instead!")
        self.redis: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.PubSub] = None

    @classmethod
    def get_instance(cls) -> 'RedisManager':
        if cls._instance is None:
            cls._instance = RedisManager()
        return cls._instance

    async def _connect(self) -> None:
        try:
            self.redis = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                decode_responses=True,
                ssl=True if os.getenv("REDIS_SSL") else False
            )
            await self.redis.ping()
            self.pubsub = self.redis.pubsub()
            logging.info("✅ تم الاتصال بـ Redis بنجاح!")
        except Exception as e:
            logging.critical(f"❌ فشل الاتصال بـ Redis: {str(e)}")
            raise

    async def connect(self) -> None:
        """Establish Redis connection"""
        if not await self.is_connected():
            await self._connect()

    async def is_connected(self) -> bool:
        """Check connection status"""
        try:
            return bool(await self.redis.ping()) if self.redis else False
        except Exception:
            return False

    async def publish_event(self, channel: str, data: dict) -> None:
        """Publish event to Redis channel"""
        if await self.is_connected():
            try:
                await self.redis.publish(channel, json.dumps(data))
                logging.info(f"📤 Event published to {channel}")
            except Exception as e:
                logging.error(f"❌ فشل نشر الحدث: {str(e)}")
        else:
            logging.warning("⚠️ Redis غير متصل!")

    async def close(self) -> None:
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
            logging.info("✅ تم إغلاق اتصال Redis")


# Initialize singleton instance
redis_manager = RedisManager.get_instance()