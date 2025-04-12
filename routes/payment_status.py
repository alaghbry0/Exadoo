from quart import Blueprint, jsonify, request, current_app
import logging
import asyncpg
from datetime import datetime

payment_status_bp = Blueprint('payment_status', __name__)

@payment_status_bp.route('/api/payment/status', methods=['GET'])
async def check_payment_status():
    """استطلاع حالة الدفع"""
    try:
        payment_token = request.args.get('token')
        
        if not payment_token:
            return jsonify({'error': 'Payment token is required'}), 400
            
        async with current_app.db_pool.acquire() as conn:
            record = await conn.fetchrow('''
                SELECT status, created_at, amount, subscription_plan_id 
                FROM payments 
                WHERE payment_token = $1
            ''', payment_token)
            
            if not record:
                return jsonify({'error': 'Payment not found'}), 404

            # تعديل حالة الدفع: استبدال "success" بـ "exchange_success"
            payment_status = record['status']
            if payment_status == 'completed':
                payment_status = 'exchange_success'
                
            # الرد بحالة الدفع والمعلومات الأخرى
            return jsonify({
                'status': payment_status,
                'created_at': record['created_at'].isoformat() if record['created_at'] else None,
                'amount': record['amount'],
                'plan_id': record['subscription_plan_id']
            }), 200
                
    except Exception as e:
        logging.error(f"Payment status check error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
