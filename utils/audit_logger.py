# =============== utils/audit_logger.py

import logging
import json
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from quart import request, current_app
from enum import Enum


class AuditSeverity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

class AuditCategory(Enum):
    USER_MANAGEMENT = "user_management"
    SUBSCRIPTION_MANAGEMENT = "subscription_management"
    ROLE_PERMISSION = "role_permission"
    SYSTEM_CONFIG = "system_config"
    SECURITY = "security"
    DATA_EXPORT = "data_export"
    BOT_MANAGEMENT = "bot_management"

class AuditLogger:
    def __init__(self):
        self.session_id = None
    
    def set_session_id(self, session_id: str):
        """تعيين معرف الجلسة للعمليات المترابطة"""
        self.session_id = session_id
    
    async def log_action(
        self,
        user_email: str,
        action: str,
        category: AuditCategory = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        resource: str = None,
        resource_id: str = None,
        details: Dict[str, Any] = None,
        old_values: Dict[str, Any] = None,
        new_values: Dict[str, Any] = None,
        ip_address: str = None,
        user_agent: str = None,
        session_id: str = None
    ):
        """تسجيل عملية في نظام الـ Audit Log"""
        
        try:
            # تحديد معرف الجلسة
            final_session_id = session_id or self.session_id or str(uuid.uuid4())
            
            # الحصول على IP والـ User Agent من الطلب الحالي
            final_ip_address = ip_address
            final_user_agent = user_agent
            
            if request:
                if not final_ip_address:
                    x_forwarded_for = request.headers.get('X-Forwarded-For')
                    if x_forwarded_for:
                        final_ip_address = x_forwarded_for.split(',')[0].strip()
                    else:
                        final_ip_address = request.remote_addr
                
                if not final_user_agent:
                    final_user_agent = request.headers.get('User-Agent')
            
            # تحويل القيم إلى JSON
            details_json = json.dumps(details) if details else None
            old_values_json = json.dumps(old_values) if old_values else None
            new_values_json = json.dumps(new_values) if new_values else None
            
            async with current_app.db_pool.acquire() as connection:
                await connection.execute("""
                    INSERT INTO audit_logs (
                        user_email, action, resource, resource_id, details,
                        old_values, new_values, ip_address, user_agent,
                        session_id, severity, category, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """, 
                user_email, action, resource, resource_id, details_json,
                old_values_json, new_values_json, final_ip_address, final_user_agent,
                final_session_id, severity.value if severity else "INFO", 
                category.value if category else None, datetime.utcnow()
                )
                
            logging.info(f"Audit log recorded: {action} by {user_email}")
            
        except Exception as e:
            logging.error(f"Failed to record audit log: {e}")
            # لا نريد أن نوقف العملية الأساسية إذا فشل التسجيل
            pass

# إنشاء مثيل عام
audit_logger = AuditLogger()