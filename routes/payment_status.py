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
            record = await conn.fetchrow('''
                SELECT 
                    p.status, 
                    p.created_at, 
                    p.amount, 
                    p.subscription_plan_id,
                    s.invite_link
                FROM payments p
                LEFT JOIN subscriptions s 
                    ON p.payment_token = s.payment_token
                WHERE p.payment_token = $1
            ''', payment_token)
            
            if not record:
                return jsonify({'error': 'Payment not found'}), 404

            payment_status = record['status']
            if payment_status == 'completed':
                payment_status = 'exchange_success'
                
            return jsonify({
                'status': payment_status,
                'created_at': record['created_at'].isoformat() if record['created_at'] else None,
                'amount': record['amount'],
                'plan_id': record['subscription_plan_id'],
                'invite_link': record['invite_link']
            }), 200
                
    except Exception as e:
        logging.error(f"Payment status check error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500