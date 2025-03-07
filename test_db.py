import asyncpg
from config import DATABASE_CONFIG
import asyncio

async def test_connection():
    try:
        conn = await asyncpg.connect(**DATABASE_CONFIG)
        print("✅ تم الاتصال بنجاح!")
        await conn.close()
    except Exception as e:
        print(f"❌ فشل الاتصال: {str(e)}")

asyncio.run(test_connection())