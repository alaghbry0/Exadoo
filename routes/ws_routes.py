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

    # تسجيل معلومات الاتصال
    client_info = {
         "remote_addr": websocket.headers.get("X-Real-IP", "Unknown"),
    "user_agent": websocket.headers.get("User-Agent", "Unknown"),
    "connected_at": datetime.now().isoformat()
}
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    connection_timestamps[ws] = time.time()

    # إرسال تأكيد الاتصال
    await ws.send(json.dumps({
        "type": "connection_established",
        "data": {
            "timestamp": datetime.now().isoformat(),
            "message": "تم إنشاء اتصال الإشعارات بنجاح"
        }
    }))

    logging.info(f"✅ تم فتح اتصال WebSocket لـ telegram_id: {telegram_id}, معلومات العميل: {client_info}")

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
            connection_timestamps[ws] = time.time()  # تحديث الطابع الزمني

            # معالجة الرسائل الواردة
            try:
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "timestamp": time.time()}))
            except json.JSONDecodeError:
                logging.warning(f"⚠️ تم استلام رسالة WebSocket غير صالحة: {message}")
    except asyncio.CancelledError:
        logging.info(f"🔌 تم إلغاء مهمة WebSocket لـ telegram_id: {telegram_id}")
    except Exception as e:
        logging.error(f"❌ خطأ في اتصال WebSocket لـ telegram_id {telegram_id}: {str(e)}")
        logging.error(traceback.format_exc())
    finally:
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
                # لا نقوم بإزالة الاتصال هنا، سيتم معالجة ذلك في مهمة فحص الاتصالات
    else:
        logging.warning(f"⚠️ لا توجد اتصالات WebSocket نشطة للمستخدم {telegram_id_str}")


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
                # لا نقوم بإزالة الاتصال هنا، سيتم معالجة ذلك في مهمة فحص الاتصالات

        logging.info(
            f"✅ تم إرسال الإشعار بنجاح إلى {successful_sends} من {len(active_connections[telegram_id_str])} اتصالات")
        return successful_sends > 0
    else:
        logging.warning(f"⚠️ لا توجد اتصالات WebSocket نشطة للمستخدم {telegram_id_str}")
        return False


async def check_connections():
    """مهمة دورية للتحقق من حالة الاتصالات وإزالة الاتصالات الميتة"""
    while True:
        try:
            current_time = time.time()
            inactive_timeout = 120  # 2 دقيقة بدون نشاط

            for telegram_id in list(active_connections.keys()):
                for ws in active_connections[telegram_id][:]:  # استخدام نسخة من القائمة
                    try:
                        # التحقق من وقت آخر نشاط
                        last_active = connection_timestamps.get(ws, 0)
                        if current_time - last_active > inactive_timeout:
                            logging.warning(
                                f"⚠️ إزالة اتصال غير نشط لـ {telegram_id} (غير نشط منذ {current_time - last_active:.1f}s)")

                            # محاولة إغلاق الاتصال بأمان
                            try:
                                await ws.close(code=1000, reason="Inactive connection")
                            except Exception:
                                pass

                            # إزالة الاتصال من القائمة
                            if ws in active_connections[telegram_id]:
                                active_connections[telegram_id].remove(ws)

                            if ws in connection_timestamps:
                                del connection_timestamps[ws]

                        # إرسال ping للتأكد من أن الاتصال لا يزال حيًا
                        elif current_time - last_active > 45:  # 45 ثانية
                            try:
                                await ws.send(json.dumps({"type": "ping", "timestamp": current_time}))
                            except Exception:
                                # محاولة إغلاق وإزالة الاتصال
                                if ws in active_connections[telegram_id]:
                                    active_connections[telegram_id].remove(ws)

                                if ws in connection_timestamps:
                                    del connection_timestamps[ws]
                    except Exception as e:
                        logging.error(f"❌ خطأ أثناء فحص الاتصال: {str(e)}")

                # إزالة المستخدمين بدون اتصالات
                if not active_connections[telegram_id]:
                    del active_connections[telegram_id]

        except Exception as e:
            logging.error(f"❌ خطأ في مهمة فحص الاتصالات: {str(e)}")

        # انتظار قبل الفحص التالي
        await asyncio.sleep(30)


# بدء مهمة فحص الاتصالات عند تسجيل البلوبرنت
@ws_bp.before_app_serving
async def start_connection_checker():
    asyncio.create_task(check_connections())