from quart import current_app


class KnowledgeBase:
    @staticmethod
    async def search(query, limit=5):
        """البحث في قاعدة المعرفة عن محتوى ذي صلة بالاستفسار"""
        try:
            # نستخدم البحث النصي البسيط في PostgreSQL
            # في المستقبل يمكن ترقيته إلى مكتبة أفضل للبحث بمعالجة اللغة العربية
            async with current_app.db_pool.acquire() as conn:
                # نستخدم ts_vector للبحث في المحتوى
                # هذا أبسط شكل للبحث النصي في PostgreSQL
                query_tokens = query.split()
                search_conditions = []

                for token in query_tokens:
                    if len(token) > 2:  # تجاهل الكلمات القصيرة جدًا
                        search_conditions.append(f"content ILIKE '%{token}%'")

                if not search_conditions:
                    return []

                search_sql = " OR ".join(search_conditions)

                rows = await conn.fetch(
                    f"""
                    SELECT id, title, content, category, tags
                    FROM knowledge_base 
                    WHERE {search_sql}
                    ORDER BY updated_at DESC
                    LIMIT $1
                    """,
                    limit
                )

                return [dict(row) for row in rows]

        except Exception as e:
            current_app.logger.error(f"خطأ في البحث بقاعدة المعرفة: {str(e)}")
            return []

    @staticmethod
    async def add_item(title, content, category=None, tags=None):
        """إضافة عنصر جديد إلى قاعدة المعرفة"""
        try:
            async with current_app.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_base (title, content, category, tags)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    title, content, category, tags
                )
                return row['id']
        except Exception as e:
            current_app.logger.error(f"خطأ في إضافة عنصر لقاعدة المعرفة: {str(e)}")
            raise

    @staticmethod
    async def update_item(item_id, title=None, content=None, category=None, tags=None):
        """تحديث عنصر موجود في قاعدة المعرفة"""
        try:
            # نبني استعلام ديناميكي للتحديث
            updates = []
            params = [item_id]
            param_index = 2  # نبدأ من 2 لأن $1 هو item_id

            if title is not None:
                updates.append(f"title = ${param_index}")
                params.append(title)
                param_index += 1

            if content is not None:
                updates.append(f"content = ${param_index}")
                params.append(content)
                param_index += 1

            if category is not None:
                updates.append(f"category = ${param_index}")
                params.append(category)
                param_index += 1

            if tags is not None:
                updates.append(f"tags = ${param_index}")
                params.append(tags)
                param_index += 1

            if not updates:
                return False

            updates.append("updated_at = CURRENT_TIMESTAMP")
            update_sql = ", ".join(updates)

            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    f"""
                    UPDATE knowledge_base
                    SET {update_sql}
                    WHERE id = $1
                    """,
                    *params
                )
                return True
        except Exception as e:
            current_app.logger.error(f"خطأ في تحديث عنصر في قاعدة المعرفة: {str(e)}")
            raise

    @staticmethod
    async def delete_item(item_id):
        """حذف عنصر من قاعدة المعرفة"""
        try:
            async with current_app.db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM knowledge_base WHERE id = $1",
                    item_id
                )
                return True
        except Exception as e:
            current_app.logger.error(f"خطأ في حذف عنصر من قاعدة المعرفة: {str(e)}")
            raise