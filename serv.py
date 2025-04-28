from aiogram import Bot
bot = Bot(token="7942179576:AAF77Tfhb8pjevtf9Qd4bsju0h0gncZjSyI")
import asyncio
async def t():
    try:
        await bot.delete_webhook()
        print("Webhook removed")
    except Exception as e:
        print("ERROR:", e)
asyncio.run(t())
