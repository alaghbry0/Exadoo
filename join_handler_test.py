import os
import logging
import asyncio
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.types import ChatJoinRequest
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter
from aiogram.enums.chat_member_status import ChatMemberStatus
from datetime import datetime, timezone
from dotenv import load_dotenv

# إعداد تسجيل الأخطاء بتفاصيل أكثر
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("join_requests.log"),
        logging.StreamHandler()
    ]
)

# تحميل متغيرات البيئة
load_dotenv()

# استخراج متغيرات البيئة
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# التحقق من وجود المتغيرات الأساسية
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ خطأ: لم يتم تعيين TELEGRAM_BOT_TOKEN")

if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD]):
    raise ValueError("❌ خطأ: لم يتم تعيين متغيرات قاعدة البيانات")

# إعداد Bot وDispatcher
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# متغير عمومي لتخزين اتصال قاعدة البيانات
db_pool = None

async def get_subscription(connection, telegram_id: int, channel_id: int):
    """
    🔹 جلب الاشتراك الحالي للمستخدم، مع التأكد من أن expiry_date هو timezone-aware.
    """
    try:
        # نلاحظ أننا نبحث في جدول subscription_types للحصول على channel_id
        subscription = await connection.fetchrow("""
            SELECT s.* FROM subscriptions s
            JOIN subscription_types st ON s.subscription_type_id = st.id
            WHERE s.telegram_id = $1 AND st.channel_id = $2
            AND s.is_active = TRUE
        """, telegram_id, channel_id)

        if subscription:
            expiry_date = subscription['expiry_date']

            # ✅ التأكد من أن expiry_date يحتوي على timezone
            if expiry_date.tzinfo is None:
                expiry_date = expiry_date.replace(tzinfo=timezone.utc)

            # ✅ مقارنة expiry_date مع الوقت الحالي الصحيح
            now_utc = datetime.now(timezone.utc)
            if expiry_date < now_utc:
                await connection.execute("""
                    UPDATE subscriptions
                    SET is_active = FALSE
                    WHERE id = $1
                """, subscription['id'])
                logging.info(f"⚠️ تم تعليم الاشتراك للمستخدم {telegram_id} في القناة {channel_id} كغير نشط.")
                return None

            return subscription

        return None  # لا يوجد اشتراك
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب الاشتراك للمستخدم {telegram_id} في القناة {channel_id}: {e}")
        return None

@dp.chat_join_request()
async def handle_join_request(join_request: ChatJoinRequest):
    """
    معالجة طلبات الانضمام للقناة والتحقق من الاشتراكات
    """
    user_id = join_request.from_user.id
    chat_id = join_request.chat.id
    username = join_request.from_user.username or "لا يوجد اسم مستخدم"
    full_name = join_request.from_user.full_name or "لا يوجد اسم كامل"
    
    logging.info(f"🔹 طلب انضمام جديد من المستخدم {user_id} (@{username} - {full_name}) إلى القناة {chat_id}")
    
    try:
        async with db_pool.acquire() as connection:
            # البحث عن اشتراك نشط للمستخدم في هذه القناة
            subscription = await get_subscription(connection, user_id, chat_id)
            
            if subscription:
                logging.info(f"✅ تم العثور على اشتراك نشط للمستخدم {user_id} في القناة {chat_id}")
                # إذا كان المستخدم يملك اشتراك نشط، قبول طلب الانضمام
                try:
                    await bot.approve_chat_join_request(
                        chat_id=chat_id,
                        user_id=user_id
                    )
                    logging.info(f"✅ تم قبول طلب انضمام المستخدم {user_id} إلى القناة {chat_id}")
                    
                    # إرسال رسالة ترحيبية للمستخدم (اختياري)
                    try:
                        await bot.send_message(
                            user_id,
                            f"✅ مرحباً {full_name}! تم قبول طلب انضمامك إلى القناة بنجاح!"
                        )
                        logging.info(f"✅ تم إرسال رسالة ترحيبية للمستخدم {user_id}")
                    except Exception as e:
                        logging.warning(f"⚠️ لم يتم إرسال رسالة الترحيب للمستخدم {user_id}: {e}")
                except Exception as e:
                    logging.error(f"❌ خطأ أثناء قبول طلب الانضمام للمستخدم {user_id}: {e}")
            else:
                logging.info(f"❌ لم يتم العثور على اشتراك نشط للمستخدم {user_id} في القناة {chat_id}")
                # إذا لم يكن هناك اشتراك نشط، رفض طلب الانضمام
                try:
                    await bot.decline_chat_join_request(
                        chat_id=chat_id,
                        user_id=user_id
                    )
                    logging.info(f"❌ تم رفض طلب انضمام المستخدم {user_id} لعدم وجود اشتراك نشط")
                    
                    # إرسال رسالة توضيحية للمستخدم (اختياري)
                    try:
                        await bot.send_message(
                            user_id,
                            "❌ لم يتم قبول طلب انضمامك للقناة لأنك لا تملك اشتراكًا نشطًا. يرجى الاشتراك أولاً."
                        )
                        logging.info(f"✅ تم إرسال رسالة رفض للمستخدم {user_id}")
                    except Exception as e:
                        logging.warning(f"⚠️ لم يتم إرسال رسالة الرفض للمستخدم {user_id}: {e}")
                except Exception as e:
                    logging.error(f"❌ خطأ أثناء رفض طلب الانضمام للمستخدم {user_id}: {e}")
                    
    except Exception as e:
        logging.error(f"❌ خطأ أثناء معالجة طلب الانضمام للمستخدم {user_id}: {e}")
        
        # في حالة حدوث خطأ، يمكن رفض الطلب كإجراء احترازي
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as decline_error:
            logging.error(f"❌ فشل رفض طلب الانضمام بعد حدوث خطأ: {decline_error}")

async def on_startup():
    """
    تنفيذ عمليات بدء التشغيل
    """
    global db_pool
    
    # إنشاء اتصال بقاعدة البيانات
    logging.info("🔄 جاري الاتصال بقاعدة البيانات...")
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        logging.info("✅ تم الاتصال بقاعدة البيانات بنجاح")
    except Exception as e:
        logging.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
        raise

    # التحقق من صلاحيات البوت
    me = await bot.get_me()
    logging.info(f"✅ تم تسجيل الدخول كـ @{me.username} (ID: {me.id})")

async def on_shutdown():
    """
    تنفيذ عمليات إيقاف التشغيل
    """
    # إغلاق اتصال قاعدة البيانات
    if db_pool:
        await db_pool.close()
        logging.info("✅ تم إغلاق اتصال قاعدة البيانات")

async def main():
    """
    الدالة الرئيسية لتشغيل البوت
    """
    try:
        await on_startup()
        logging.info("🚀 جاري تشغيل البوت للتعامل مع طلبات الانضمام...")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ حدث خطأ أثناء تشغيل البوت: {e}")
    finally:
        await on_shutdown()

if __name__ == "__main__":
    logging.info("🚀 بدء تشغيل معالج طلبات الانضمام")
    asyncio.run(main())