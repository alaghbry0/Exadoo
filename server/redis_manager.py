# server/redis_manager.py
import redis.asyncio as redis
import os
import json
import logging
import time
from typing import Optional
from redis.exceptions import ConnectionError, TimeoutError, RedisError

class RedisManager:
    _instance: Optional['RedisManager'] = None

    def __init__(self):
        if RedisManager._instance is not None:
            raise RuntimeError("Use get_instance() instead!")
        self.redis: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None

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
                password=os.getenv("REDIS_PASSWORD"),  # إضافة كلمة المرور
                db=int(os.getenv("REDIS_DB", 0)),
                decode_responses=True,
                ssl=True if os.getenv("REDIS_SSL") else False
            )
            await self.redis.ping()
            self.pubsub = self.redis.pubsub()
            logging.info("✅ تم الاتصال بـ Redis بنجاح!")

        except ConnectionError as e:
            logging.critical(f"❌ فشل الاتصال بـ Redis: خطأ في الاتصال - {str(e)}")
            raise
        except TimeoutError as e:
            logging.critical(f"❌ فشل الاتصال بـ Redis: المهلة انتهت - {str(e)}")
            raise
        except RedisError as e:
            logging.critical(f"❌ فشل Redis بسبب خطأ عام - {str(e)}")
            raise
        except Exception as e:
            logging.critical(f"❌ خطأ غير متوقع أثناء الاتصال بـ Redis: {str(e)}")
            raise

    async def connect(self) -> None:
        """Establish Redis connection"""
        if not await self.is_connected():
            await self._connect()

    async def is_connected(self) -> bool:
        """Check connection status"""
        try:
            return bool(await self.redis.ping()) if self.redis else False
        except (ConnectionError, TimeoutError, RedisError):
            return False

    async def publish_event(self, channel: str, data: dict):
        try:
            # إنشاء تسلسل فريد لكل قناة
            seq_key = f"event_seq:{channel}"
            seq = await self.redis.incr(seq_key)

            event_data = {
                **data,
                "_seq": seq,
                "_ts": time.time()
            }

            await self.redis.publish(channel, json.dumps(event_data))
            logging.debug(f"تم نشر الحدث: {event_data}")

        except (ConnectionError, TimeoutError) as e:
            logging.error(f"⚠️ خطأ في الاتصال أثناء النشر: {str(e)}")
            raise
        except json.JSONDecodeError as e:
            logging.error(f"⚠️ خطأ في تحويل البيانات إلى JSON: {str(e)}")
            raise
        except RedisError as e:
            logging.error(f"⚠️ خطأ Redis أثناء النشر: {str(e)}")
            raise
        except Exception as e:
            logging.error(f"⚠️ خطأ غير متوقع أثناء النشر: {str(e)}")
            raise

    async def close(self) -> None:
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
            logging.info("✅ تم إغلاق اتصال Redis")

# Initialize singleton instance
redis_manager = RedisManager.get_instance()
