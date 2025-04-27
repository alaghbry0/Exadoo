#chat_manager.py
import re
import asyncio
import json
import time
from typing import List, Dict, Any, Optional


class ChatManager:
    def __init__(self, app=None):
        self.db_pool = None
        self.logger = None
        self.kv_cache: Dict[str, Any] = {}
        if app:
            self.init_app(app)

    def init_app(self, app):
        """Initialize with application dependencies."""
        self.db_pool = app.db_pool
        self.logger = app.logger

    async def get_conversation_history(self, session_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get conversation history for a session with proper message format for the API.
        Returns a list of messages [{"role": ..., "content": ...}, ...].
        """
        try:
            async with self.db_pool.acquire() as conn:
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
            messages: List[Dict[str, Any]] = []
            for row in reversed(rows):
                messages.append({"role": "user", "content": row["user_message"]})
                if row["tool_calls"]:
                    messages.append({
                        "role": "assistant",
                        "content": row["bot_response"],
                        "tool_calls": row["tool_calls"]
                    })
                else:
                    messages.append({"role": "assistant", "content": row["bot_response"]})
            return messages
        except Exception as e:
            self.logger.error(f"Error retrieving conversation history: {e}")
            return []

    async def get_conversation_with_cache(self, session_id: str, limit: int = 5) -> Dict[str, Any]:
        """
        Get conversation history with KV cache information.
        Returns {"history": [...], "kv_cache_id": ...}.
        """
        kv_cache_id = self.kv_cache.get(session_id)
        if not kv_cache_id:
            try:
                async with self.db_pool.acquire() as conn:
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
                    self.kv_cache[session_id] = kv_cache_id
            except Exception as e:
                self.logger.error(f"Error retrieving KV cache: {e}")

        history = await self.get_conversation_history(session_id, limit)
        return {"history": history, "kv_cache_id": kv_cache_id}

    async def update_kv_cache(self, session_id: str, kv_cache_id: str):
        """
        Update the KV cache ID for a session in memory and persistent storage.
        """
        self.kv_cache[session_id] = kv_cache_id
        try:
            async with self.db_pool.acquire() as conn:
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
            self.logger.error(f"Error saving KV cache: {e}")

    async def save_conversation(self, user_id: str, session_id: str,
                                user_message: str, bot_response: Any,
                                knowledge_ids: Optional[List[int]] = None,
                                tool_calls: Optional[List[Dict[str, Any]]] = None) -> str:
        """
        Save the conversation to the database.
        Supports tool_calls tracking.
        Returns the conversation record ID.
        """
        try:
            # Convert knowledge_ids to a list if it's not
            if knowledge_ids is None:
                knowledge_ids = []

            # Ensure tool_calls is properly handled
            if tool_calls is not None:
                if isinstance(tool_calls, list):
                    tool_calls_jsonb = json.dumps(tool_calls)  # تحويل القائمة إلى نص JSON
                else:
                    tool_calls_jsonb = tool_calls
            else:
                tool_calls_jsonb = None

            async with self.db_pool.acquire() as conn:
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
            self.logger.error(f"Error saving conversation: {e}")
            return ""

    async def update_conversation_feedback(self, conversation_id: int,
                                           rating: Optional[int] = None,
                                           feedback: Optional[str] = None) -> bool:
        """
        Update conversation with user feedback and rating.
        """
        try:
            if rating is not None and (rating < 1 or rating > 5):
                self.logger.error(f"Invalid rating value: {rating}")
                return False

            async with self.db_pool.acquire() as conn:
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
            self.logger.error(f"Error updating conversation feedback: {e}")
            return False

    async def update_knowledge_scores(self, conversation_id: int,
                                      knowledge_scores: str) -> bool:
        """
        Update conversation with knowledge scores information.
        """
        try:
            async with self.db_pool.acquire() as conn:
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
            self.logger.error(f"Error updating knowledge scores: {e}")
            return False

    async def load_kv_cache(self):
        """
        Load KV cache IDs from database at startup.
        """
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT session_id, kv_cache_id 
                    FROM session_cache 
                    WHERE updated_at > NOW() - INTERVAL '1 day'
                    """
                )
            for row in rows:
                self.kv_cache[row["session_id"]] = row["kv_cache_id"]
            self.logger.info(f"Loaded {len(rows)} KV cache IDs")
        except Exception as e:
            self.logger.error(f"Error loading KV cache: {e}")

    @staticmethod
    def _sanitize_input(text: Optional[str]) -> Optional[str]:
        """
        Sanitize input text to prevent injection attacks.
        """
        if not text:
            return text
        return re.sub(r"[\'\"\\;]", "", text)

    async def save_feedback(self, conversation_id: Any, rating: Any,
                            feedback: Optional[str] = None) -> bool:
        """
        Save user feedback for a conversation.
        """
        try:
            try:
                conv_id = int(conversation_id)
            except (ValueError, TypeError):
                raise ValueError("Invalid conversation_id")
            rating_int = None
            if rating is not None:
                rating_int = int(rating)
                if rating_int < 1 or rating_int > 5:
                    raise ValueError("Rating must be between 1 and 5")
            sanitized_feedback = self._sanitize_input(feedback) if feedback else None

            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE conversations
                    SET rating = $1, feedback = $2
                    WHERE id = $3
                    """,
                    rating_int, sanitized_feedback, conv_id
                )
            return True
        except Exception as e:
            self.logger.error(f"Error saving feedback: {e}")
            return False
