from quart import current_app
import re


class ChatManager:
    @staticmethod
    async def save_conversation(user_id, session_id, user_message, bot_response, knowledge_ids=None):
        """حفظ المحادثة في قاعدة البيانات"""
        try:
            # تنظيف المدخلات
            user_id = ChatManager._sanitize_input(user_id)
            session_id = ChatManager._sanitize_input(session_id)
            user_message = ChatManager._sanitize_input(user_message)
            bot_response = ChatManager._sanitize_input(bot_response)

            async with current_app.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO conversations (user_id, session_id, user_message, bot_response, knowledge_ids)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    user_id, session_id, user_message, bot_response, knowledge_ids
                )
                return row['id']
        except Exception as e:
            current_app.logger.error(f"خطأ في حفظ المحادثة: {str(e)}")
            raise

    @staticmethod
    async def save_feedback(conversation_id, rating, feedback=None):
        """حفظ تقييم المستخدم للمحادثة"""
        try:
            # تحويل معرف المحادثة إلى رقم لمنع حقن SQL
            try:
                conversation_id_int = int(conversation_id)
            except (ValueError, TypeError):
                raise ValueError("معرف المحادثة غير صالح")

            # تنظيف المدخلات
            feedback = ChatManager._sanitize_input(feedback) if feedback else None

            # التحقق من صحة التقييم
            try:
                rating_int = int(rating)
                if rating_int not in range(1, 6):  # تقييم من 1 إلى 5
                    raise ValueError("التقييم يجب أن يكون بين 1 و 5")
            except (ValueError, TypeError):
                raise ValueError("التقييم غير صالح")

            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE conversations
                    SET rating = $1, feedback = $2
                    WHERE id = $3
                    """,
                    rating_int, feedback, conversation_id_int
                )
                return True
        except Exception as e:
            current_app.logger.error(f"خطأ في حفظ التقييم: {str(e)}")
            raise

    @staticmethod
    async def get_conversation_history(session_id, limit=5):
        """الحصول على سجل المحادثات السابقة للمستخدم في الجلسة الحالية"""
        try:
            # تنظيف المدخلات
            session_id = ChatManager._sanitize_input(session_id)

            async with current_app.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, user_message, bot_response, created_at
                    FROM conversations
                    WHERE session_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    session_id, limit
                )
                return [dict(row) for row in rows]
        except Exception as e:
            current_app.logger.error(f"خطأ في استرجاع سجل المحادثات: {str(e)}")
            return []

    @staticmethod
    def _sanitize_input(text):
        """تنظيف النص المدخل لمنع هجمات الحقن"""
        if not text:
            return text

        # إزالة أي محاولات حقن SQL
        text = re.sub(r'[\'"\\;]', '', text)
        return text