from quart import Quart, websocket, request, jsonify
import asyncio
import json

app = Quart(__name__)

# قاموس لتخزين الاتصالات المفتوحة حسب telegram_id
active_connections = {}

# يمكنك استخدام هذه الدالة للتحقق من صحة telegram_id
async def validate_telegram_id(telegram_id):
    # في بيئة الإنتاج، سيكون هنا التحقق من API الخاص بـ Telegram أو قاعدة البيانات
    # للتبسيط، سنقبل أي telegram_id غير فارغ
    return telegram_id is not None and telegram_id != ""



@app.route('/')
async def hello():
    return {"status": "WebSocket server is running"}


@app.websocket('/ws/<telegram_id>')
async def ws(telegram_id):
    # التحقق من صحة telegram_id
    if not await validate_telegram_id(telegram_id):
        return "Invalid Telegram ID", 401

    # تخزين الاتصال في القاموس
    active_connections[telegram_id] = websocket._get_current_object()

    try:
        while True:
            # انتظار رسالة من العميل
            data = await websocket.receive()

            try:
                # تحويل البيانات المستلمة من JSON إلى كائن Python
                message = json.loads(data)
                print(f"Received message from {telegram_id}: {message}")

                # إرسال تأكيد استلام
                await websocket.send(json.dumps({
                    "status": "received",
                    "message": message
                }))

                # هنا يمكنك إضافة منطق إضافي للتعامل مع الرسائل

            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "status": "error",
                    "message": "Invalid JSON format"
                }))
    except asyncio.CancelledError:
        # تنظيف عند إغلاق الاتصال
        print(f"Connection closed for {telegram_id}")
    finally:
        # إزالة الاتصال من القائمة النشطة عند انتهاء الاتصال
        if telegram_id in active_connections:
            del active_connections[telegram_id]


# وظيفة خدمية لإرسال رسالة إلى مستخدم معين عبر WebSocket
@app.route('/send/<telegram_id>', methods=['POST'])
async def send_message(telegram_id):
    if telegram_id not in active_connections:
        return jsonify({"status": "error", "message": "User not connected"}), 404

    data = await request.get_json()
    message = data.get('message', '')

    try:
        await active_connections[telegram_id].send(json.dumps({
            "status": "message",
            "content": message
        }))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)