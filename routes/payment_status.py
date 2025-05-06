from quart import Blueprint, jsonify, request, current_app
import logging
import asyncpg
from datetime import datetime

payment_status_bp = Blueprint('payment_status', __name__)

# في ملف الخادم
@payment_status_bp.route('/api/payment/status', methods=['GET'])
async def check_payment_status():
    """استطلاع حالة الدفع"""
    try:
        payment_token = request.args.get('token')
        
        if not payment_token:
            return jsonify({'error': 'Payment token is required'}), 400
            
        async with current_app.db_pool.acquire() as conn:
            # الحصول على بيانات الدفع الأساسية
            payment_record = await conn.fetchrow('''
                SELECT p.status, p.created_at, p.amount, p.subscription_plan_id, p.telegram_id
                FROM payments p
                WHERE p.payment_token = $1
            ''', payment_token)
            
            if not payment_record:
                return jsonify({'error': 'Payment not found'}), 404

            # البحث عن الاشتراك مع إعادة المحاولة لمدة 5 ثواني
            max_retries = 5
            invite_link = None
            
            for _ in range(max_retries):
                subscription_record = await conn.fetchrow('''
    SELECT s.invite_link 
    FROM subscriptions s
    WHERE 
        s.subscription_type_id = $1 AND
        s.telegram_id = $2
    ORDER BY s.start_date DESC
    LIMIT 1
''', payment_record['subscription_plan_id'], payment_record['telegram_id'])
                
                if subscription_record and subscription_record['invite_link']:
                    invite_link = subscription_record['invite_link']
                    break
                    
                await asyncio.sleep(1)  # انتظار 1 ثانية قبل إعادة المحاولة

            # تعديل حالة الدفع
            payment_status = payment_record['status']
            if payment_status == 'completed':
                payment_status = 'exchange_success'
                
            return jsonify({
                'status': payment_status,
                'created_at': payment_record['created_at'].isoformat() if payment_record['created_at'] else None,
                'amount': payment_record['amount'],
                'plan_id': payment_record['subscription_plan_id'],
                'invite_link': invite_link
            }), 200
                
    except Exception as e:
        logging.error(f"Payment status check error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500