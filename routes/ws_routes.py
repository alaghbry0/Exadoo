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
    if not await validate_telegram_id(telegram_id):
        logging.error(f"❌ Invalid Telegram ID: {telegram_id}")
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    # إضافة الاتصال للقائمة
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"✅ WebSocket connection established for telegram_id: {telegram_id}")

    # إرسال رسالة تأكيد الاتصال
    try:
        await ws.send(json.dumps({
            "type": "connection_established",
            "data": {"status": "connected"}
        }))
    except Exception as e:
        logging.error(f"❌ Error sending confirmation: {e}")

    # إرسال عدد الإشعارات غير المقروءة فور الاتصال
    try:
        # استعلام عدد الإشعارات غير المقروءة من قاعدة البيانات
        async with current_app.db_pool.acquire() as connection:
            query = """
                SELECT COUNT(*) AS unread_count
                FROM user_notifications
                WHERE telegram_id = $1 AND read_status = FALSE;
            """
            result = await connection.fetchrow(query, int(telegram_id))
            unread_count = result["unread_count"] if result else 0

        await ws.send(json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        }))
    except Exception as e:
        logging.error(f"❌ Error sending initial unread count: {e}")

    # إضافة نبض للحفاظ على الاتصال
    ping_task = None
    try:
        # تعريف وظيفة النبض
        async def ping_client():
            while True:
                await asyncio.sleep(30)  # إرسال نبض كل 30 ثانية
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break

        # بدء مهمة النبض
        ping_task = asyncio.create_task(ping_client())

        # انتظار الرسائل من العميل
        while True:
            data = await websocket.receive()
            logging.info(f"🔄 Received data from {telegram_id}: {data}")

            try:
                msg_data = json.loads(data)
                if msg_data.get("type") == "pong":
                    continue  # تجاهل رسائل الرد على النبض
                # معالجة الرسائل الأخرى
            except json.JSONDecodeError:
                logging.warning(f"Invalid JSON received: {data}")

            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        logging.info(f"WebSocket task cancelled for {telegram_id}")
    except Exception as e:
        logging.error(f"❌ Error in WebSocket connection for {telegram_id}: {e}")
        logging.error(traceback.format_exc())
    finally:
        # إلغاء مهمة النبض إذا كانت موجودة
        if ping_task:
            ping_task.cancel()

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
async def broadcast_unread_count(telegram_id, unread_count):
    """
    إرسال تحديث لعدد الإشعارات غير المقروءة للمستخدم

    :param telegram_id: معرف التلغرام للمستخدم
    :param unread_count: عدد الإشعارات غير المقروءة
    :return: None
    """
    telegram_id_str = str(telegram_id)

    if telegram_id_str in active_connections:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })

        tasks = []
        for ws in active_connections[telegram_id_str]:
            tasks.append(ws.send(message))

        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                logging.info(f"✅ Unread count update sent to {telegram_id}")
            except Exception as e:
                logging.error(f"❌ Failed to send unread count to {telegram_id}: {e}")
    else:
        logging.info(f"⚠️ No active connections for {telegram_id}")


# وظيفة لإرسال إشعار للمستخدم
async def broadcast_notification(telegram_id, notification_data):
    """
    إرسال إشعار للمستخدم عبر WebSocket

    :param telegram_id: معرف التلغرام للمستخدم
    :param notification_data: بيانات الإشعار (قاموس)
    :return: bool - نجاح الإرسال
    """
    sent_successfully = False
    telegram_id_str = str(telegram_id)

    if telegram_id_str in active_connections:
        message = json.dumps({
            "type": "notification",
            "data": notification_data
        })

        # استخدام asyncio.gather للإرسال المتوازي لجميع الاتصالات
        tasks = []
        for ws in active_connections[telegram_id_str]:
            tasks.append(ws.send(message))

        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                sent_successfully = True
                logging.info(f"✅ Notification sent to {telegram_id}")
            except Exception as e:
                logging.error(f"❌ Failed to send notification to {telegram_id}: {e}")
    else:
        logging.info(f"⚠️ No active connections for {telegram_id}")

    return sent_successfully