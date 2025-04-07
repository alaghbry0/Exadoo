import asyncio
import websockets
import json
import sys


async def connect_and_listen(telegram_id):
    uri = f"wss://exadoo-rxr9.onrender.com/ws/notifications/{telegram_id}"

    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected with telegram_id: {telegram_id}")

            # تشغيل مهام متزامنة: إرسال واستقبال الرسائل
            await asyncio.gather(
                send_messages(websocket),
                receive_messages(websocket)
            )
    except Exception as e:
        print(f"Error: {e}")


async def send_messages(websocket):
    try:
        while True:
            message = input("Enter message (or 'exit' to quit): ")
            if message.lower() == 'exit':
                break

            await websocket.send(json.dumps({
                "type": "message",
                "content": message
            }))
            await asyncio.sleep(0.1)  # انتظار قصير لتجنب استهلاك الموارد
    except Exception as e:
        print(f"Send error: {e}")


async def receive_messages(websocket):
    try:
        while True:
            response = await websocket.recv()
            print(f"Received: {response}")
    except Exception as e:
        print(f"Receive error: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python client.py <telegram_id>")
        sys.exit(1)

    telegram_id = sys.argv[1]
    asyncio.run(connect_and_listen(telegram_id))