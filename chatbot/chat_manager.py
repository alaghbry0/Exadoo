import re
import asyncio
from quart import current_app
import json
import time
from typing import List, Dict, Any, Optional


class ChatManager:
    def __init__(self):
        # Initialize connection to database via pool
        self.kv_cache = {}  # KV Cache ID for each session

    async def get_conversation_history(self, session_id: str, limit: int = 5) -> List[Dict[str, str]]:
        """
        Get conversation history for a session with proper message format for the API
        Returns: List of messages in the format [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        """
        try:
            async with current_app.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT user_message, bot_response, timestamp, tool_calls
                    FROM conversations 
                    WHERE session_id = $1 
                    ORDER BY timestamp DESC 
                    LIMIT $2
                    """,
                    session_id, limit
                )

                # Convert to messages format
                messages = []
                for row in reversed(rows):  # Reverse to get chronological order
                    messages.append({"role": "user", "content": row["user_message"]})

                    # Check if there are tool calls for this assistant message
                    if row["tool_calls"]:
                        tool_calls = row["tool_calls"]
                        assistant_message = {
                            "role": "assistant",
                            "content": row["bot_response"],
                            "tool_calls": tool_calls
                        }
                        messages.append(assistant_message)
                    else:
                        messages.append({"role": "assistant", "content": row["bot_response"]})

                return messages
        except Exception as e:
            current_app.logger.error(f"Error retrieving conversation history: {e}")
            return []

    async def get_conversation_with_cache(self, session_id: str, limit: int = 5):
        """Get conversation history with KV cache information"""
        # First try to get KV cache ID from memory
        kv_cache_id = self.kv_cache.get(session_id)

        # If not found in memory, try to get from database
        if not kv_cache_id:
            try:
                async with current_app.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT kv_cache_id 
                        FROM session_cache 
                        WHERE session_id = $1
                        """,
                        session_id
                    )
                    if row:
                        kv_cache_id = row["kv_cache_id"]
                        # Update in-memory cache
                        self.kv_cache[session_id] = kv_cache_id
            except Exception as e:
                current_app.logger.error(f"Error retrieving KV cache: {e}")

        # Get conversation history
        history = await self.get_conversation_history(session_id, limit)

        return {
            "history": history,
            "kv_cache_id": kv_cache_id
        }

    async def update_kv_cache(self, session_id: str, kv_cache_id: str):
        """Update the KV cache ID for a session"""
        self.kv_cache[session_id] = kv_cache_id

        # Also save to a more persistent storage if needed
        try:
            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO session_cache (session_id, kv_cache_id, updated_at) 
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (session_id) 
                    DO UPDATE SET kv_cache_id = $2, updated_at = NOW()
                    """,
                    session_id, kv_cache_id
                )
        except Exception as e:
            current_app.logger.error(f"Error saving KV cache: {e}")

    async def save_conversation(self, user_id: str, session_id: str,
                                user_message: str, bot_response: str,
                                knowledge_ids: List[int] = None,
                                tool_calls: List[Dict] = None) -> str:
        """
        Save the conversation to the database
        Now also supports tool_calls for tracking function calling
        """
        try:
            # Format tool_calls as JSONB for PostgreSQL
            tool_calls_jsonb = tool_calls

            # If bot_response is a dict, extract content
            if isinstance(bot_response, dict):
                if "tool_calls" in bot_response and not tool_calls:
                    tool_calls_jsonb = bot_response.get("tool_calls")
                bot_response = bot_response.get("content", "")

            async with current_app.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO conversations
                        (user_id, session_id, user_message, bot_response, 
                         knowledge_ids, tool_calls, timestamp)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    RETURNING id
                    """,
                    user_id, session_id, user_message, bot_response,
                    knowledge_ids, tool_calls_jsonb
                )
                return str(row["id"])
        except Exception as e:
            current_app.logger.error(f"Error saving conversation: {e}")
            return ""

    async def update_conversation_feedback(self, conversation_id: int, rating: int = None,
                                           feedback: str = None) -> bool:
        """
        Update conversation with user feedback and rating
        """
        try:
            # Validate rating is between 1 and 5
            if rating is not None and (rating < 1 or rating > 5):
                current_app.logger.error(f"Invalid rating value: {rating}")
                return False

            async with current_app.db_pool.acquire() as conn:
                if rating is not None and feedback is not None:
                    await conn.execute(
                        """
                        UPDATE conversations
                        SET rating = $1, feedback = $2
                        WHERE id = $3
                        """,
                        rating, feedback, conversation_id
                    )
                elif rating is not None:
                    await conn.execute(
                        """
                        UPDATE conversations
                        SET rating = $1
                        WHERE id = $2
                        """,
                        rating, conversation_id
                    )
                elif feedback is not None:
                    await conn.execute(
                        """
                        UPDATE conversations
                        SET feedback = $1
                        WHERE id = $2
                        """,
                        feedback, conversation_id
                    )
                else:
                    return False

                return True
        except Exception as e:
            current_app.logger.error(f"Error updating conversation feedback: {e}")
            return False

    async def update_knowledge_scores(self, conversation_id: int, knowledge_scores: str) -> bool:
        """
        Update conversation with knowledge scores information
        """
        try:
            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE conversations
                    SET knowledge_scores = $1
                    WHERE id = $2
                    """,
                    knowledge_scores, conversation_id
                )
                return True
        except Exception as e:
            current_app.logger.error(f"Error updating knowledge scores: {e}")
            return False

    async def load_kv_cache(self):
        """Load KV cache IDs from database at startup"""
        try:
            async with current_app.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT session_id, kv_cache_id 
                    FROM session_cache 
                    WHERE updated_at > NOW() - INTERVAL '1 day'
                    """
                )
                for row in rows:
                    self.kv_cache[row["session_id"]] = row["kv_cache_id"]

                current_app.logger.info(f"Loaded {len(rows)} KV cache IDs")
        except Exception as e:
            current_app.logger.error(f"Error loading KV cache: {e}")


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
    def _sanitize_input(text):
        """تنظيف النص المدخل لمنع هجمات الحقن"""
        if not text:
            return text

        # إزالة أي محاولات حقن SQL
        text = re.sub(r'[\'"\\;]', '', text)
        return text