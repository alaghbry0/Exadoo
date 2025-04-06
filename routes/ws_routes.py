# server/ws_routes.py
from quart import Blueprint, websocket, request
import asyncio
import json
import logging
import traceback
import weakref

ws_bp = Blueprint('ws_bp', __name__)

# استخدام weakref لتجنب تسريب الذاكرة مع اتصالات WebSocket المفتوحة
active_connections = {}


@ws_bp.websocket('/ws/notifications')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not telegram_id or not telegram_id.isdigit():
        logging.warning(f"❌ محاولة اتصال WebSocket بدون telegram_id صالح")
        await websocket.close(code=4000, reason="Missing or invalid telegram_id")
        return

    # التأكد من أن telegram_id هو دائمًا string
    telegram_id = str(telegram_id)
    ws = websocket._get_current_object()

    if telegram_id not in active_connections:
        active_connections[telegram_id] = []

    # إضافة الاتصال إلى قائمة الاتصالات النشطة
    active_connections[telegram_id].append(ws)
    conn_count = len(active_connections[telegram_id])
    logging.info(f"✅ تم فتح اتصال WebSocket لـ telegram_id: {telegram_id} (اتصال #{conn_count})")

    try:
        # إرسال رسالة تأكيد الاتصال
        await ws.send(json.dumps({
            "type": "connection_established",
            "data": {
                "status": "connected",
                "message": "تم الاتصال بنجاح"
            }
        }))

        # انتظار الرسائل من العميل (مثل ping)
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=60)
                if data:
                    try:
                        message = json.loads(data)
                        if message.get('type') == 'ping':
                            await ws.send(json.dumps({"type": "pong"}))
                    except json.JSONDecodeError:
                        logging.warning(f"⚠️ تم استلام رسالة WebSocket غير صالحة من {telegram_id}")
            except asyncio.TimeoutError:
                # إرسال pong للحفاظ على الاتصال
                try:
                    await ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    # إذا فشل إرسال pong، نخرج من الحلقة
                    break
    except asyncio.CancelledError:
        logging.info(f"🔌 تم إلغاء اتصال WebSocket لـ telegram_id: {telegram_id}")
    except Exception as e:
        logging.error(f"❌ خطأ في اتصال WebSocket لـ {telegram_id}: {e}")
        logging.error(traceback.format_exc())
    finally:
        if telegram_id in active_connections:
            try:
                active_connections[telegram_id].remove(ws)
                if not active_connections[telegram_id]:
                    del active_connections[telegram_id]
                logging.info(
                    f"🔌 تم قطع اتصال WebSocket لـ telegram_id: {telegram_id} (متبقي: {len(active_connections.get(telegram_id, []))})")
            except ValueError:
                logging.warning(f"⚠️ محاولة إزالة اتصال WebSocket غير موجود لـ {telegram_id}")
                pass


# تحسين وظيفة البث للتأكد من أن الرسائل تُرسل بشكل آمن
async def _send_message_safe(ws, message):
    try:
        await ws.send(message)
        return True
    except Exception as e:
        logging.error(f"❌ فشل إرسال الرسالة: {e}")
        return False


def broadcast_unread_count(telegram_id, unread_count):
    telegram_id = str(telegram_id)  # تأكيد التحويل إلى string
    if telegram_id in active_connections and active_connections[telegram_id]:
        message = json.dumps({
            "type": "unread_update",
            "data": {"count": unread_count}
        })
        logging.info(
            f"📤 محاولة بث عدد الإشعارات غير المقروءة ({unread_count}) لـ {telegram_id} ({len(active_connections[telegram_id])} اتصالات)")

        # إنشاء مهام مستقلة لكل اتصال
        tasks = [
            asyncio.create_task(_send_message_safe(ws, message))
            for ws in active_connections[telegram_id]
        ]

        # يمكننا انتظار اكتمال جميع المهام إذا كنا نريد تأكيدًا
        # await asyncio.gather(*tasks, return_exceptions=True)
    else:
        logging.warning(f"⚠️ لا يوجد اتصالات WebSocket نشطة للمستخدم {telegram_id}")


async def broadcast_notification(telegram_id, notification_data):
    """
    وظيفة مساعدة لبث إشعار عبر WebSocket

    :param telegram_id: معرف المستخدم (سيتم تحويله إلى نص)
    :param notification_data: بيانات الإشعار (dict ستحول إلى JSON)
    """
    telegram_id = str(telegram_id)
    if telegram_id in active_connections and active_connections[telegram_id]:
        message = json.dumps(notification_data)
        logging.info(f"📤 محاولة بث إشعار لـ {telegram_id} ({len(active_connections[telegram_id])} اتصالات)")

        success_count = 0
        for ws in active_connections[telegram_id]:
            if await _send_message_safe(ws, message):
                success_count += 1

        logging.info(f"✅ تم إرسال الإشعار بنجاح لـ {success_count}/{len(active_connections[telegram_id])} اتصالات")
        return success_count > 0
    else:
        logging.warning(f"⚠️ لا يوجد اتصالات WebSocket نشطة للمستخدم {telegram_id}")
        return False