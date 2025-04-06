# server/ws_routes.py
from quart import Blueprint, websocket, request, jsonify
import asyncio
import json
import logging
import traceback

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}

# دالة للتحقق من صحة telegram_id
async def validate_telegram_id(telegram_id):
    # في بيئة الإنتاج يمكن التحقق من API Telegram أو قاعدة البيانات
    return telegram_id is not None and telegram_id.strip() != '' and telegram_id.isdigit()


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not is_valid_telegram_id(telegram_id):
        logging.error(f"❌ Invalid Telegram ID: {telegram_id}")
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    # إضافة الاتصال للقائمة
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"✅ WebSocket connection established for telegram_id: {telegram_id}")

    try:
        while True:
            data = await websocket.receive()  # انتظار استقبال رسالة
            logging.info(f"🔄 Received data from {telegram_id}: {data}")
            # هنا يمكن إضافة منطق لمعالجة الرسالة
            await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"❌ Error in WebSocket connection for {telegram_id}: {e}")
        logging.error(traceback.format_exc())
    finally:
        # تنظيف الاتصال عند الإغلاق
        if telegram_id in active_connections:
            try:
                active_connections[telegram_id].remove(ws)
                if not active_connections[telegram_id]:
                    del active_connections[telegram_id]
            except ValueError:
                pass
        logging.info(f"🔌 WebSocket connection closed for telegram_id: {telegram_id}")

# وظيفة خدمية لإرسال رسالة إلى مستخدم معين عبر WebSocket
def broadcast_unread_count(telegram_id, unread_count):
    if str(telegram_id) in active_connections:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })
        for ws in active_connections[str(telegram_id)]:
            try:
                asyncio.create_task(ws.send(message))
                logging.info(f"✅ Message sent to {telegram_id}")
            except Exception as e:
                logging.error(f"❌ Failed to send message to {telegram_id}: {e}")
