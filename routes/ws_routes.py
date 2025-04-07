# server/ws_routes.py

from quart import Blueprint, websocket, request, jsonify, current_app
import asyncio
import json
import logging
import time
import traceback
from database.db_queries import get_unread_notifications_count

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}
# تخزين آخر نشاط للمستخدم
last_activity = {}
# مدة انتهاء الجلسة (بالثواني)
SESSION_TIMEOUT = 3600  # ساعة واحدة

# دالة للتحقق من صحة telegram_id
async def validate_telegram_id(telegram_id):
    # في بيئة الإنتاج يمكن التحقق من API Telegram أو قاعدة البيانات
    return telegram_id is not None and telegram_id.strip() != '' and telegram_id.isdigit()

# دالة لتنظيف الاتصالات غير النشطة
async def cleanup_inactive_connections():
    while True:
        try:
            current_time = time.time()
            inactive_ids = []
            for telegram_id, last_time in list(last_activity.items()):
                if current_time - last_time > SESSION_TIMEOUT:
                    inactive_ids.append(telegram_id)
            for telegram_id in inactive_ids:
                if telegram_id in active_connections:
                    for ws in active_connections[telegram_id]:
                        try:
                            await ws.close(1000, "Session timeout")
                        except Exception:
                            pass
                    del active_connections[telegram_id]
                if telegram_id in last_activity:
                    del last_activity[telegram_id]
                logging.info(f"🧹 Cleaned up inactive connection for {telegram_id}")
        except Exception as e:
            logging.error(f"❌ Error in cleanup task: {e}")
        await asyncio.sleep(300)  # تشغيل كل 5 دقائق

# بدء مهمة التنظيف
cleanup_task = None

@ws_bp.before_app_serving
async def before_serving():
    global cleanup_task
    cleanup_task = asyncio.create_task(cleanup_inactive_connections())
    logging.info("✅ Started WebSocket cleanup task")

@ws_bp.after_app_serving
async def after_serving():
    if cleanup_task:
        cleanup_task.cancel()
    logging.info("🛑 Stopped WebSocket cleanup task")


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not await validate_telegram_id(telegram_id):
        logging.error(f"❌ Invalid Telegram ID: {telegram_id}")
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    # تحديث وقت آخر نشاط
    last_activity[telegram_id] = time.time()

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

    # إرسال عدد الإشعارات غير المقروءة فور الاتصال باستخدام دالة من db_queries.py
    async def send_initial_unread_count():
        # التقاط نسخة من التطبيق
        app = current_app._get_current_object()
        async with app.db_pool.acquire() as connection:
            unread_count = await get_unread_notifications_count(connection, int(telegram_id))
        await ws.send(json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        }))

    try:
        await send_initial_unread_count()
    except Exception as e:
        logging.error(f"❌ Error sending initial unread count: {e}")

    # إضافة نبض للحفاظ على الاتصال
    ping_task = None
    try:
        async def ping_client():
            ping_interval = 30  # إرسال نبض كل 30 ثانية
            missed_pings = 0
            max_missed_pings = 3  # أقصى عدد للنبضات المفقودة
            while True:
                try:
                    # تحديث وقت آخر نشاط عند كل نبض
                    last_activity[telegram_id] = time.time()
                    await ws.send(json.dumps({"type": "ping", "timestamp": time.time()}))
                    await asyncio.sleep(ping_interval)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning(f"⚠️ Ping failed for {telegram_id}: {e}")
                    missed_pings += 1
                    if missed_pings >= max_missed_pings:
                        logging.error(f"❌ Too many missed pings for {telegram_id}, closing connection")
                        break
                    await asyncio.sleep(ping_interval)
        # بدء مهمة النبض
        ping_task = asyncio.create_task(ping_client())

        # انتظار الرسائل من العميل
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=120)
                # تحديث وقت آخر نشاط عند استلام أي رسالة
                last_activity[telegram_id] = time.time()
                try:
                    msg_data = json.loads(data)
                    if msg_data.get("type") == "pong":
                        continue  # تجاهل رسائل الرد على النبض
                    # معالجة الرسائل الأخرى هنا إذا لزم الأمر
                except json.JSONDecodeError:
                    logging.warning(f"Invalid JSON received: {data}")
            except asyncio.TimeoutError:
                if telegram_id in last_activity and time.time() - last_activity[telegram_id] > SESSION_TIMEOUT:
                    logging.info(f"🔌 Session timeout for {telegram_id}")
                    break
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"❌ Error receiving message from {telegram_id}: {e}")
                break
    except asyncio.CancelledError:
        logging.info(f"WebSocket task cancelled for {telegram_id}")
    except Exception as e:
        logging.error(f"❌ Error in WebSocket connection for {telegram_id}: {e}")
        logging.error(traceback.format_exc())
    finally:
        if ping_task:
            ping_task.cancel()
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
    """
    telegram_id_str = str(telegram_id)
    if telegram_id_str in active_connections:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })
        tasks = [ws.send(message) for ws in active_connections[telegram_id_str]]
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