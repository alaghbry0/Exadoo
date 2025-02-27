import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def test_connection():
    try:
        print("🔄 محاولة الاتصال بقاعدة البيانات...")
        conn = await asyncpg.connect(
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            database=os.getenv('DB_NAME'),
            host=os.getenv('DB_HOST'),
            port=int(os.getenv('DB_PORT', 5432)),
            ssl="require",
            statement_cache_size=0
        )
        print("✅ Connection successful!")
        await conn.close()
        print("🔴 Connection closed.")
    except Exception as e:
        print(f"🚨 خطأ أثناء الاتصال: {e}")

print("⚡ Running test_connection...")  # ✅ هذا سيظهر إذا تم تشغيل السكريبت
asyncio.run(test_connection())
