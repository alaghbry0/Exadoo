from quart import current_app
import json

class ChatManager:
    @staticmethod
    async def save_conversation(user_id, session_id, user_message, bot_response, knowledge_sources=None):
        """حفظ المحادثة والرسائل في قاعدة البيانات"""
        try:
            async with current_app.db_pool.acquire() as conn:
                # التحقق مما إذا كانت المحادثة موجودة بالفعل
                conversation = await conn.fetchrow(
                    """
                    SELECT id FROM chat_conversations 
                    WHERE session_id = $1 
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    session_id
                )

                if conversation:
                    conversation_id = conversation['id']
                    # تحديث وقت آخر نشاط للمحادثة
                    await conn.execute(
                        "UPDATE chat_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = $1",
                        conversation_id
                    )
                else:
                    # إنشاء محادثة جديدة مع تحويل metadata إلى JSON string
                    metadata_str = json.dumps({"source": "web"})
                    conversation_id = await conn.fetchval(
                        """
                        INSERT INTO chat_conversations (user_id, session_id, metadata)
                        VALUES ($1, $2, $3::jsonb)
                        RETURNING id
                        """,
                        user_id, session_id, metadata_str
                    )

                # حفظ رسالة المستخدم
                await conn.execute(
                    """
                    INSERT INTO chat_messages (conversation_id, is_user, content)
                    VALUES ($1, $2, $3)
                    """,
                    conversation_id, True, user_message
                )

                # حفظ رد البوت
                await conn.execute(
                    """
                    INSERT INTO chat_messages (conversation_id, is_user, content, knowledge_sources)
                    VALUES ($1, $2, $3, $4)
                    """,
                    conversation_id, False, bot_response, knowledge_sources or []
                )

                return conversation_id

        except Exception as e:
            current_app.logger.error(f"خطأ في حفظ المحادثة: {str(e)}")
            raise

    @staticmethod
    async def get_conversation_history(session_id, limit=50):
        """استرجاع تاريخ المحادثة"""
        try:
            async with current_app.db_pool.acquire() as conn:
                conversation = await conn.fetchrow(
                    "SELECT id FROM chat_conversations WHERE session_id = $1",
                    session_id
                )

                if not conversation:
                    return []

                conversation_id = conversation['id']

                rows = await conn.fetch(
                    """
                    SELECT is_user, content, created_at
                    FROM chat_messages
                    WHERE conversation_id = $1
                    ORDER BY created_at ASC
                    LIMIT $2
                    """,
                    conversation_id, limit
                )

                history = []
                for row in rows:
                    history.append({
                        'is_user': row['is_user'],
                        'content': row['content'],
                        'timestamp': row['created_at'].isoformat()
                    })

                return history

        except Exception as e:
            current_app.logger.error(f"خطأ في استرجاع تاريخ المحادثة: {str(e)}")
            return []

    @staticmethod
    async def save_feedback(conversation_id, rating, feedback=''):
        """حفظ تقييم المستخدم للمحادثة"""
        try:
            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_performance (conversation_id, rating, feedback)
                    VALUES ($1, $2, $3)
                    """,
                    conversation_id, rating, feedback
                )
                return True
        except Exception as e:
            current_app.logger.error(f"خطأ في حفظ التقييم: {str(e)}")
            raise