import logging
import pytz
import os
import json
from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta, timezone
from database.db_queries import (
    get_user, add_user, add_subscription, update_subscription, add_scheduled_task
)
from utils.db_utils import generate_channel_invite_link, send_message_to_user

# نفترض أنك قد أنشأت وحدة خاصة بالإشعارات تحتوي على الدالة create_notification
from utils.notifications import create_notification

# إنشاء Blueprint لنقاط API الخاصة بالاشتراكات
subscriptions_bp = Blueprint("subscriptions", __name__)

LOCAL_TZ = pytz.timezone("Asia/Riyadh")  # التوقيت المحلي
IS_DEVELOPMENT = True  # يمكن تغييره بناءً على بيئة التطبيق
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # مفتاح Webhook


async def calculate_subscription_dates(connection, telegram_id, main_channel_id, duration_days, duration_minutes_dev,
                                       current_time_utc):
    """Helper function to calculate start and expiry dates."""
    existing_main_channel_sub = await connection.fetchrow(
        "SELECT id, start_date, expiry_date, is_active FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
        telegram_id, main_channel_id
    )

    start_date = current_time_utc
    base_expiry = current_time_utc

    if existing_main_channel_sub and \
            existing_main_channel_sub['is_active'] and \
            existing_main_channel_sub['expiry_date'] >= current_time_utc:
        start_date = existing_main_channel_sub['start_date']
        base_expiry = existing_main_channel_sub['expiry_date']

    new_expiry_date = base_expiry + timedelta(days=duration_days, minutes=duration_minutes_dev)
    return start_date, new_expiry_date


@subscriptions_bp.route("/api/subscribe", methods=["POST"])
async def subscribe():
    """
    API endpoint for renewing a subscription or creating a new one after payment confirmation.
    - Only the main channel is recorded in the 'subscriptions' table.
    - 'remove_user' tasks for secondary channels are scheduled programmatically.
    - Reminders ('first_reminder', 'second_reminder') are handled by a DB trigger
      acting on the main channel's record in 'subscriptions'.
    """
    try:
        secret = request.headers.get("Authorization")
        if not secret or secret != f"Bearer {WEBHOOK_SECRET}":
            logging.warning("❌ Unauthorized request: Invalid or missing WEBHOOK_SECRET")
            return jsonify({"error": "Unauthorized request"}), 403

        data = await request.get_json()
        logging.info(f"📥 Received subscription request: {json.dumps(data, indent=2)}")

        telegram_id = data.get("telegram_id")
        subscription_plan_id_from_request = data.get("subscription_plan_id")
        payment_id_from_request = data.get("payment_id")  # This is the tx_hash
        payment_token = data.get("payment_token")
        username = data.get("username", None)
        full_name = data.get("full_name", None)

        if not (isinstance(telegram_id, int) and
                isinstance(subscription_plan_id_from_request, int) and
                isinstance(payment_id_from_request, str)):
            logging.error(
                f"❌ Invalid data format for basic fields. TG_ID: {telegram_id}, Plan_ID: {subscription_plan_id_from_request}, Payment_ID: {payment_id_from_request}")
            return jsonify({"error": "Invalid data format"}), 400

        if not payment_token:
            logging.error("❌ Missing payment_token in the request data")
            return jsonify({"error": "Missing payment_token"}), 400

        db_pool = getattr(current_app, "db_pool", None)
        if not db_pool:
            logging.critical("❌ Database connection is missing!")
            return jsonify({"error": "Internal Server Error"}), 500

        async with db_pool.acquire() as connection:
            # --- Preliminary checks and fetching initial data ---
            subscription_plan = await connection.fetchrow(
                "SELECT id, subscription_type_id, name, duration_days FROM subscription_plans WHERE id = $1",
                subscription_plan_id_from_request
            )
            if not subscription_plan:
                logging.error(f"❌ Invalid subscription_plan_id: {subscription_plan_id_from_request}")
                return jsonify({"error": "Invalid subscription plan."}), 400

            current_subscription_type_id = subscription_plan["subscription_type_id"]
            duration_days = subscription_plan["duration_days"]

            subscription_type_info = await connection.fetchrow(
                "SELECT id, name, channel_id AS main_channel_id FROM subscription_types WHERE id = $1",
                current_subscription_type_id
            )
            if not subscription_type_info:
                logging.error(
                    f"❌ Invalid subscription_type_id: {current_subscription_type_id} for plan {subscription_plan_id_from_request}")
                return jsonify({"error": "Invalid subscription type."}), 400

            main_channel_id = int(subscription_type_info["main_channel_id"]) if subscription_type_info[
                "main_channel_id"] else None
            if not main_channel_id:
                logging.critical(
                    f"❌ Critical: No main_channel_id configured for subscription_type_id: {current_subscription_type_id}")
                return jsonify({"error": "Subscription type is not configured with a main channel."}), 400

            subscription_type_name = subscription_type_info["name"]

            # --- Check if payment and main channel subscription already fully processed ---
            # This check assumes that if a payment record exists in 'payments' AND
            # an active subscription for the main channel with this payment_id exists,
            # the process was likely completed before.
            payment_check_query = """
                SELECT p.id FROM payments p WHERE p.tx_hash = $1;
            """
            payment_record_exists = await connection.fetchval(payment_check_query, payment_id_from_request)

            if payment_record_exists:
                existing_main_channel_subscription = await connection.fetchrow(
                    "SELECT id FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2 AND payment_id = $3 AND is_active = TRUE",
                    telegram_id, main_channel_id, payment_id_from_request
                )
                if existing_main_channel_subscription:
                    logging.info(
                        f"✅ Main channel subscription for {main_channel_id} with payment_id {payment_id_from_request} already active for user {telegram_id}. Assuming fully processed.")
                    return jsonify({"message": "Subscription already active and likely fully processed."}), 200
                else:
                    logging.info(
                        f"ℹ️ Payment {payment_id_from_request} exists, but main channel subscription is not active or found. Proceeding to create/renew.")
            # If payment_record_exists is False, we assume this webhook should create the subscription.
            # The responsibility of creating the record in the 'payments' table itself is outside this function's scope,
            # or should be handled by the webhook source if it's the first time seeing this payment.

            await add_user(connection, telegram_id, username=username, full_name=full_name)

            all_channels_for_type = await connection.fetch(
                "SELECT channel_id, channel_name, is_main FROM subscription_type_channels WHERE subscription_type_id = $1 ORDER BY is_main DESC, channel_name",
                current_subscription_type_id
            )
            if not all_channels_for_type:
                logging.error(
                    f"❌ No channels in 'subscription_type_channels' for type_id: {current_subscription_type_id}")
                return jsonify({"error": "No channels are associated with this subscription type."}), 400

            if not any(c['channel_id'] == main_channel_id and c['is_main'] for c in all_channels_for_type):
                logging.critical(
                    f"❌ Config error: Main channel {main_channel_id} not listed as 'is_main=true' in subscription_type_channels for type {current_subscription_type_id}.")
                return jsonify({"error": "Configuration error with main channel association."}), 500

            current_time_utc = datetime.now(timezone.utc)
            duration_minutes_dev = 120 if IS_DEVELOPMENT else 0

            main_invite_link_generated = None
            main_subscription_record_id_for_history = None
            # processed_main_channel_subscription will be true if main channel subscription record is handled
            processed_main_channel_subscription = False
            secondary_channel_links_to_send = []
            total_channels_links_generated = 0

            calculated_start_date, calculated_new_expiry_date = await calculate_subscription_dates(
                connection, telegram_id, main_channel_id, duration_days, duration_minutes_dev, current_time_utc
            )

            # --- Process each channel associated with the subscription type ---
            for channel_data in all_channels_for_type:
                current_channel_id_being_processed = int(channel_data["channel_id"])
                current_channel_name = channel_data["channel_name"] or f"Channel {current_channel_id_being_processed}"
                is_current_channel_main = channel_data["is_main"]

                logging.info(
                    f"Processing channel: {current_channel_name} (ID: {current_channel_id_being_processed}, Main: {is_current_channel_main}) for user {telegram_id}")

                invite_result = await generate_channel_invite_link(telegram_id, current_channel_id_being_processed,
                                                                   current_channel_name)
                if not invite_result["success"]:
                    logging.error(
                        f"❌ Failed to generate invite link for {current_channel_name}: {invite_result.get('error')}")
                    if is_current_channel_main:
                        logging.critical(
                            f"CRITICAL: Could not generate invite link for MAIN channel {main_channel_id}. Aborting subscription process.")
                        return jsonify(
                            {"error": "Failed to process main channel invite link. Subscription aborted."}), 500
                    logging.warning(f"Skipping secondary channel {current_channel_name} due to invite link failure.")
                    continue  # Skip this secondary channel

                current_channel_invite_link = invite_result["invite_link"]
                total_channels_links_generated += 1

                if is_current_channel_main:
                    main_invite_link_generated = current_channel_invite_link

                    existing_main_sub_record = await connection.fetchrow(
                        "SELECT id FROM subscriptions WHERE telegram_id = $1 AND channel_id = $2",
                        # channel_id is main_channel_id here
                        telegram_id, main_channel_id
                    )

                    if existing_main_sub_record:
                        success_update = await update_subscription(
                            # ensure update_subscription returns a boolean or raises error
                            connection, telegram_id, main_channel_id, current_subscription_type_id,
                            subscription_plan_id_from_request, calculated_new_expiry_date, calculated_start_date,
                            True, payment_id_from_request, main_invite_link_generated
                            # Always update main channel's invite link
                        )
                        if not success_update:
                            logging.critical(f"❌ Failed to update main subscription record for {telegram_id}")
                            return jsonify({"error": "Failed to update main subscription record."}), 500
                        main_subscription_record_id_for_history = existing_main_sub_record['id']
                        logging.info(
                            f"🔄 Main subscription renewed for user {telegram_id} in channel {current_channel_name} until {calculated_new_expiry_date}")
                    else:
                        newly_created_main_sub_id = await add_subscription(
                            connection, telegram_id, main_channel_id, current_subscription_type_id,
                            subscription_plan_id_from_request, calculated_start_date, calculated_new_expiry_date,
                            True, payment_id_from_request, main_invite_link_generated,
                            returning_id=True
                        )
                        if not newly_created_main_sub_id:
                            logging.critical(f"❌ Failed to create main subscription record for {telegram_id}")
                            return jsonify({"error": "Failed to create main channel subscription record."}), 500
                        main_subscription_record_id_for_history = newly_created_main_sub_id
                        logging.info(
                            f"✅ New main subscription created (ID: {newly_created_main_sub_id}) for user {telegram_id} in channel {current_channel_name}")

                    processed_main_channel_subscription = True

                else:  # This is a secondary channel
                    secondary_channel_links_to_send.append(
                        f"▫️ قناة <a href='{current_channel_invite_link}'>{current_channel_name}</a>"
                    )
                    # Schedule 'remove_user' task for this secondary channel programmatically
                    # Delete any old 'remove_user' task for this user and secondary channel to avoid duplicates on renewal
                    await connection.execute(
                        "DELETE FROM scheduled_tasks WHERE task_type = 'remove_user' AND telegram_id = $1 AND channel_id = $2",
                        telegram_id, current_channel_id_being_processed
                    )
                    await add_scheduled_task(
                        connection, "remove_user", telegram_id,
                        current_channel_id_being_processed, calculated_new_expiry_date
                    )
                    logging.info(
                        f"📅 Programmatically scheduled 'remove_user' for SECONDARY channel {current_channel_name} (ID: {current_channel_id_being_processed}) at {calculated_new_expiry_date.astimezone(LOCAL_TZ)}")

            # --- End of channels loop ---

            if not processed_main_channel_subscription:
                logging.critical(
                    f"❌ Main channel subscription record was NOT processed for user {telegram_id}, plan_id {subscription_plan_id_from_request}.")
                return jsonify({"error": "Failed to process the main channel subscription record."}), 500

            if not main_invite_link_generated:
                logging.critical(
                    f"❌ CRITICAL: Main invite link was NOT generated (should have been if main channel processed). User {telegram_id}, Plan {subscription_plan_id_from_request}.")
                return jsonify({"error": "Main invite link was not generated despite main channel processing."}), 500

            if not main_subscription_record_id_for_history:
                main_sub_rec_check = await connection.fetchrow(
                    "SELECT id FROM subscriptions WHERE telegram_id=$1 AND channel_id=$2 AND payment_id=$3",
                    telegram_id, main_channel_id, payment_id_from_request)
                if main_sub_rec_check:
                    main_subscription_record_id_for_history = main_sub_rec_check['id']
                else:
                    logging.critical(
                        f"CRITICAL: Could not confirm main_subscription_record_id for history. User {telegram_id}, Main Ch {main_channel_id}")
                    return jsonify({"error": "Failed to confirm main subscription record ID for history log."}), 500

            if secondary_channel_links_to_send:
                secondary_links_message_text = (
                        f"📬 مرحبًا {full_name or username or telegram_id},\n\n"
                        f"اشتراكك في \"{subscription_type_name}\" مفعل الآن!\n"
                        "بالإضافة إلى القناة الرئيسية، يمكنك الانضمام إلى القنوات الفرعية التالية:\n\n" +
                        "\n".join(secondary_channel_links_to_send) +
                        "\n\n💡 هذه الروابط خاصة بك وصالحة لفترة محدودة. يرجى الانضمام في أقرب وقت."
                )
                await send_message_to_user(telegram_id, secondary_links_message_text)

            previous_history_exists = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM subscription_history WHERE telegram_id=$1 AND subscription_type_name=$2 AND action_type IN ('NEW', 'RENEWAL') AND payment_id = $3)",
                # Added payment_id for more specific check
                telegram_id, subscription_type_name, payment_id_from_request
            )
            action_type_for_history = 'RENEWAL' if previous_history_exists else 'NEW'

            extra_data_for_history = json.dumps({
                "full_name": full_name, "username": username,
                "main_channel_subscription_handled": processed_main_channel_subscription,
                # True if main channel sub record was touched
                "total_channels_in_bundle": len(all_channels_for_type),
                "secondary_links_generated_count": len(secondary_channel_links_to_send),
                "payment_id_ref": payment_id_from_request  # Storing tx_hash for reference
            })

            history_query = """
                INSERT INTO subscription_history (
                    subscription_id, invite_link, action_type, subscription_type_name, subscription_plan_name,
                    renewal_date, expiry_date, telegram_id, extra_data, payment_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id 
                -- Added payment_id to history table
            """
            # Assuming subscription_history table has a 'payment_id' column to store tx_hash
            history_record = await connection.fetchrow(
                history_query, main_subscription_record_id_for_history, main_invite_link_generated,
                action_type_for_history, subscription_type_name, subscription_plan["name"],
                calculated_start_date, calculated_new_expiry_date, telegram_id, extra_data_for_history,
                payment_id_from_request  # Store the tx_hash in history
            )
            if not history_record:
                logging.error(f"❌ Failed to insert into subscription_history for payment {payment_id_from_request}")
                # Decide if this is a critical failure
            else:
                subscription_history_id = history_record["id"]

            final_expiry_date_local = calculated_new_expiry_date.astimezone(LOCAL_TZ)
            final_start_date_local = calculated_start_date.astimezone(LOCAL_TZ)

            notification_title = f"{'تجديد' if action_type_for_history == 'RENEWAL' else 'تفعيل'} اشتراك: {subscription_type_name}"
            # تعديل الرسالة لتعكس عدد القنوات الكلي
            num_accessible_channels = 1 + len(secondary_channel_links_to_send) if main_invite_link_generated else len(
                secondary_channel_links_to_send)

            notification_message_text = (
                f"🎉 تم بنجاح {'تجديد' if action_type_for_history == 'RENEWAL' else 'تفعيل'} اشتراكك في \"{subscription_type_name}\"!\n"
                f"صالح حتى: {final_expiry_date_local.strftime('%Y-%m-%d %H:%M %Z')}.\n"
                f"يمكنك الآن الوصول إلى {num_accessible_channels} قناة."
            )

            extra_notification_data = {
                "subscription_type": subscription_type_name,
                "subscription_history_id": subscription_history_id if 'subscription_history_id' in locals() else None,
                "expiry_date_iso": calculated_new_expiry_date.isoformat(),
                "start_date_iso": calculated_start_date.isoformat(),
                "main_invite_link": main_invite_link_generated,
                "payment_token": payment_token,
                "secondary_links_sent_count": len(secondary_channel_links_to_send)
            }
            await create_notification(
                connection=connection, notification_type="subscription_update", title=notification_title,
                message=notification_message_text, extra_data=extra_notification_data,
                is_public=False, telegram_ids=[telegram_id]
            )

            formatted_response_message_html = (
                f"✅ تم {'تجديد' if action_type_for_history == 'RENEWAL' else 'تفعيل'} اشتراكك في \"{subscription_type_name}\" بنجاح!<br>"
                f"ينتهي في: {final_expiry_date_local.strftime('%Y-%m-%d %H:%M:%S %Z')}.<br>"
                f"🔗 للانضمام إلى القناة الرئيسية: <a href='{main_invite_link_generated}' target='_blank'>اضغط هنا</a>."
            )
            if secondary_channel_links_to_send:
                formatted_response_message_html += "<br>📬 تم إرسال روابط القنوات الفرعية إليك عبر رسالة من البوت. يرجى التحقق من رسائلك."

            return jsonify({
                "message": notification_message_text,
                "expiry_date_formatted": final_expiry_date_local.strftime('%Y-%m-%d %H:%M:%S %Z'),
                "start_date_formatted": final_start_date_local.strftime('%Y-%m-%d %H:%M:%S %Z'),
                "main_invite_link": main_invite_link_generated,
                "formatted_message": formatted_response_message_html
            }), 200

    except Exception as e:
        logging.error(f"❌ Critical error in /api/subscribe endpoint: {str(e)}", exc_info=True)
        error_message = "An internal server error occurred." if not IS_DEVELOPMENT else str(e)
        return jsonify({"error": error_message}), 500