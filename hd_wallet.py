import asyncio
import logging
from aiogram import Bot

# إعداد سجل الأخطاء
logging.basicConfig(level=logging.INFO)

# استبدل هذا برمز التوكن الخاص ببوتك
API_TOKEN = '7781104897:AAGDhDdlVFuR3GfDUsl-5oKTwJOxoRSDTiY'

# معرف القناة الذي تريد إنشاء رابط الدعوة لها (يجب أن يكون بصيغة -100XXXXXXXXXX)
CHANNEL_ID = -10022775553158

async def main():
    bot = Bot(token=API_TOKEN)
    try:
        # إنشاء رابط دعوة للقناة مع الإعدادات التالية:
        # creates_join_request=True لجعل طلب الانضمام يحتاج لموافقة المشرفين
        # عدم تحديد expire_date يجعل مدة صلاحية الرابط غير محدودة
        invite_link = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            creates_join_request=True
        )
        print("رابط الدعوة:", invite_link.invite_link)
    except Exception as e:
        logging.error(f"حدث خطأ أثناء إنشاء رابط الدعوة: {e}")
    finally:
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
