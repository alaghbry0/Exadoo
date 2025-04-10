# server/ws_routes.py

from quart import Blueprint, websocket, request, jsonify, current_app
import asyncio
import json
import logging
import time
import traceback
from database.db_queries import get_unread_notifications_count

ws_bp = Blueprint('ws_bp', __name__)

# Store active connections by telegram_id
active_connections = {}
# Store last activity timestamp
last_activity = {}
# Session timeout in seconds
SESSION_TIMEOUT = 3600  # 1 hour

# Function to validate telegram_id
async def validate_telegram_id(telegram_id):
    # In production, you might validate against Telegram API or database
    return telegram_id is not None and telegram_id.strip() != '' and telegram_id.isdigit()

# Task to clean up inactive connections
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
        await asyncio.sleep(300)  # Run every 5 minutes

# Start cleanup task
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

# Mark all notifications as read for a user
async def mark_all_notifications_read(telegram_id):
    """
    Mark all notifications as read for a specific user

    :param telegram_id: Telegram ID of the user
    :return: Number of updated notifications
    """
    app = current_app._get_current_object()
    async with app.db_pool.acquire() as connection:
        update_query = """
            UPDATE user_notifications
            SET read_status = TRUE
            WHERE telegram_id = $1 AND read_status = FALSE
            RETURNING notification_id
        """
        updated_rows = await connection.fetch(update_query, int(telegram_id))
        updated_count = len(updated_rows)
        
        # Get updated unread count
        unread_count = await get_unread_notifications_count(connection, int(telegram_id))
        
        # Broadcast updated count to all active connections
        await broadcast_unread_count(telegram_id, unread_count)
        
        logging.info(f"✅ Marked {updated_count} notifications as read for {telegram_id}")
        return updated_count

@ws_bp.websocket('/ws')
async def notifications_ws():
    telegram_id = websocket.args.get('telegram_id')
    if not await validate_telegram_id(telegram_id):
        logging.error(f"❌ Invalid Telegram ID: {telegram_id}")
        await websocket.close(code=4000)
        return

    ws = websocket._get_current_object()

    # Update last activity time
    last_activity[telegram_id] = time.time()

    # Add connection to list
    if telegram_id not in active_connections:
        active_connections[telegram_id] = []
    active_connections[telegram_id].append(ws)
    logging.info(f"✅ WebSocket connection established for telegram_id: {telegram_id}")

    # Send confirmation message
    try:
        await ws.send(json.dumps({
            "type": "connection_established",
            "data": {"status": "connected"}
        }))
    except Exception as e:
        logging.error(f"❌ Error sending confirmation: {e}")

    # Send initial unread count
    async def send_initial_unread_count():
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

    # Add heartbeat to keep connection alive
    ping_task = None
    try:
        async def ping_client():
            ping_interval = 30  # Send ping every 30 seconds
            missed_pings = 0
            max_missed_pings = 3  # Maximum number of missed pings
            while True:
                try:
                    # Update last activity time with each ping
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
        # Start ping task
        ping_task = asyncio.create_task(ping_client())

        # Wait for messages from client
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=120)
                # Update last activity time when receiving any message
                last_activity[telegram_id] = time.time()
                try:
                    msg_data = json.loads(data)
                    if msg_data.get("type") == "pong":
                        continue  # Ignore ping response messages
                    
                    # Handle mark_as_read messages
                    if msg_data.get("type") == "mark_as_read":
                        # Mark all notifications as read for this user
                        updated_count = await mark_all_notifications_read(telegram_id)
                        # Confirm to client
                        await ws.send(json.dumps({
                            "type": "notifications_marked_read",
                            "data": {"count": updated_count}
                        }))
                    
                    # Handle other message types here
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

# Utility function to send a message to a specific user via WebSocket
async def broadcast_unread_count(telegram_id, unread_count):
    """
    Send an update of unread notifications count to the user

    :param telegram_id: Telegram ID of the user
    :param unread_count: Number of unread notifications
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

# Function to send notification to a user
async def broadcast_notification(telegram_id, notification_data, notification_type):
    """
    Send a notification to the user via WebSocket

    :param telegram_id: Telegram ID of the user
    :param notification_data: Notification data (dictionary)
    :param notification_type: Type of notification
    :return: bool - success of sending
    """
    sent_successfully = False
    telegram_id_str = str(telegram_id)

    if telegram_id_str in active_connections:
        message = json.dumps({
            "type": notification_type,
            "data": notification_data
        })

        # Use asyncio.gather for parallel sending to all connections
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

# New function to broadcast new notification to a user
async def broadcast_new_notification(telegram_id, notification):
    """
    Send a new notification to the user via WebSocket

    :param telegram_id: Telegram ID of the user
    :param notification: Notification object
    :return: bool - success of sending
    """
    app = current_app._get_current_object()
    async with app.db_pool.acquire() as connection:
        # Get updated unread count
        unread_count = await get_unread_notifications_count(connection, int(telegram_id))
        
        # First update the unread count
        await broadcast_unread_count(telegram_id, unread_count)
        
        # Then send the notification itself
        return await broadcast_notification(
            telegram_id, 
            notification, 
            'new_notification'
        )
    

