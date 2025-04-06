# server/ws_routes.py
from quart import Blueprint, websocket, request, current_app
import asyncio
import json
import logging
import traceback
from datetime import datetime
import time

ws_bp = Blueprint('ws_bp', __name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}
# قاموس لتخزين آخر وقت تلقي رسالة من كل اتصال
connection_timestamps = {}
# قاموس لتخزين مهام ping الدورية
ping_tasks = {}


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not telegram_id:
        logging.warning("❌ محاولة اتصال بدون معرف telegram_id")
        await websocket.close(code=4000, reason="Missing telegram_id")
        return

    # تحويل telegram_id إلى سلسلة نصية للاتساق
    telegram_id = str(telegram_id)
    ws = websocket._get_current_object()

    # تسجيل معلومات المتصفح للتشخيص
    user_agent = request.headers.get('User-Agent', 'غير معروف')
    logging.info(f"🌐 محاولة اتصال من: {user_agent} للمستخدم {telegram_id}")

    # إغلاق الاتصالات المكررة لنفس المستخدم
    # إذا كان هناك أكثر من 2 اتصالات، قم بإغلاق الأقدم
    if telegram_id in active_connections and len(active_connections[telegram_id]) >= 2:
        logging.warning(f"⚠️ تم اكتشاف اتصالات متعددة لـ {telegram_id}، سيتم إغلاق أقدم اتصال")
        try:
            oldest_ws = active_connections[telegram_id][0]
            active_connections[telegram_id].remove(oldest_ws)
            await oldest_ws.close(code=1000, reason="Too many connections")
            if oldest_ws in connection_timestamps:
                del connection_timestamps[oldest_ws]
        except Exception as e:
            logging.error(f"❌ خطأ أثناء إغلاق الاتصال القديم: {str(e)}")

    # إضافة الاتصال الجديد
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    connection_timestamps[ws] = time.time()

    # إنشاء مهمة ping فردية لهذا الاتصال
    async def ping_client():
        try:
            ping_interval = 15  # 15 ثانية
            while ws in connection_timestamps:
                await asyncio.sleep(ping_interval)
                try:
                    if ws not in connection_timestamps:
                        break
                    # إرسال ping فقط إذا كان الاتصال لا يزال مفتوحًا
                    await ws.send(json.dumps({"type": "ping", "timestamp": time.time()}))
                    logging.debug(f"📍 تم إرسال ping إلى {telegram_id}")
                except Exception as e:
                    logging.warning(f"⚠️ فشل إرسال ping: {str(e)}")
                    # إزالة الاتصال إذا فشل ping
                    if telegram_id in active_connections and ws in active_connections[telegram_id]:
                        active_connections[telegram_id].remove(ws)
                        if ws in connection_timestamps:
                            del connection_timestamps[ws]
                        if not active_connections[telegram_id]:
                            del active_connections[telegram_id]
                    break
        except asyncio.CancelledError:
            logging.debug(f"🔌 تم إلغاء مهمة ping لـ {telegram_id}")
        except Exception as e:
            logging.error(f"❌ خطأ في مهمة ping: {str(e)}")

    # بدء مهمة ping وتخزين المرجع
    ping_task = asyncio.create_task(ping_client())
    if telegram_id not in ping_tasks:
        ping_tasks[telegram_id] = []
    ping_tasks[telegram_id].append(ping_task)

    # إرسال تأكيد الاتصال
    await ws.send(json.dumps({
        "type": "connection_established",
        "data": {
            "timestamp": datetime.now().isoformat(),
            "message": "تم إنشاء اتصال الإشعارات بنجاح"
        }
    }))

    logging.info(f"✅ تم فتح اتصال WebSocket لـ telegram_id: {telegram_id}")

    # إرسال عدد الإشعارات غير المقروءة عند الاتصال
    try:
        # الحصول على عدد الإشعارات غير المقروءة
        db_pool = getattr(current_app, "db_pool", None)
        if db_pool:
            async with db_pool.acquire() as connection:
                unread_query = """
                    SELECT COUNT(*) AS unread_count
                    FROM user_notifications
                    WHERE telegram_id = $1 AND read_status = FALSE;
                """
                result = await connection.fetchrow(unread_query, int(telegram_id))
                unread_count = result["unread_count"] if result else 0

                await ws.send(json.dumps({
                    "type": "unread_update",
                    "data": {"count": unread_count}
                }))
    except Exception as e:
        logging.error(f"❌ خطأ في جلب عدد الإشعارات غير المقروءة: {str(e)}")

    try:
        while True:
            message = await websocket.receive()
            current_time = time.time()
            connection_timestamps[ws] = current_time  # تحديث الطابع الزمني

            # معالجة الرسائل الواردة
            try:
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "timestamp": current_time}))
                    logging.debug(f"🏓 تم الرد على ping من {telegram_id}")
            except json.JSONDecodeError:
                logging.warning(f"⚠️ تم استلام رسالة WebSocket غير صالحة: {message}")
    except asyncio.CancelledError:
        logging.info(f"🔌 تم إلغاء مهمة WebSocket لـ telegram_id: {telegram_id}")
    except Exception as e:
        logging.error(f"❌ خطأ في اتصال WebSocket لـ telegram_id {telegram_id}: {str(e)}")
        logging.error(traceback.format_exc())
    finally:
        # إلغاء مهمة ping
        if telegram_id in ping_tasks:
            for task in ping_tasks[telegram_id]:
                if task == ping_task:
                    task.cancel()
                    ping_tasks[telegram_id].remove(task)
                    break
            if not ping_tasks[telegram_id]:
                del ping_tasks[telegram_id]

        # تنظيف الاتصال عند الإغلاق
        if telegram_id in active_connections and ws in active_connections[telegram_id]:
            active_connections[telegram_id].remove(ws)
            if not active_connections[telegram_id]:
                del active_connections[telegram_id]

            if ws in connection_timestamps:
                del connection_timestamps[ws]

            logging.info(f"🔌 تم قطع اتصال WebSocket لـ telegram_id: {telegram_id}")


async def broadcast_unread_count(telegram_id, unread_count):
    """إرسال تحديث لعدد الإشعارات غير المقروءة لمستخدم محدد"""
    # تحويل telegram_id إلى string للتأكد من الاتساق
    telegram_id_str = str(telegram_id)

    if telegram_id_str in active_connections:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })

        active_clients = len(active_connections[telegram_id_str])
        logging.info(
            f"📤 إرسال تحديث عدد الإشعارات غير المقروءة ({unread_count}) إلى {telegram_id_str} ({active_clients} اتصالات نشطة)")

        for ws in active_connections[telegram_id_str][:]:  # استخدام نسخة من القائمة
            try:
                await ws.send(message)
                logging.debug(f"✅ تم إرسال تحديث عدد الإشعارات بنجاح إلى {telegram_id_str}")
            except Exception as e:
                logging.error(f"❌ فشل إرسال تحديث عدد الإشعارات إلى {telegram_id_str}: {str(e)}")
    else:
        logging.debug(f"⚠️ لا توجد اتصالات WebSocket نشطة للمستخدم {telegram_id_str}")


async def broadcast_notification(telegram_id, notification_data):
    """إرسال إشعار إلى مستخدم محدد"""
    # تحويل telegram_id إلى string للتأكد من الاتساق
    telegram_id_str = str(telegram_id)

    if telegram_id_str in active_connections:
        message = json.dumps(notification_data)

        logging.info(
            f"📤 محاولة إرسال إشعار إلى {telegram_id_str}, عدد الاتصالات: {len(active_connections[telegram_id_str])}")

        successful_sends = 0
        for ws in active_connections[telegram_id_str][:]:  # استخدام نسخة من القائمة
            try:
                await ws.send(message)
                successful_sends += 1
            except Exception as e:
                logging.error(f"❌ فشل إرسال الإشعار إلى اتصال WebSocket: {str(e)}")

        logging.info(
            f"✅ تم إرسال الإشعار بنجاح إلى {successful_sends} من {len(active_connections[telegram_id_str])} اتصالات")
        return successful_sends > 0
    else:
        logging.debug(f"⚠️ لا توجد اتصالات WebSocket نشطة للمستخدم {telegram_id_str}")
        return False