from quart import current_app
import re

class KnowledgeBase:

    @staticmethod
    async def search(query: str, limit: int = 5):
        q = KnowledgeBase._sanitize_query(query)
        if not q:
            return []

        emb = await current_app.embedding_service.get_embedding(q)
        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, content, category, tags,
                       embedding_vector <#> $1 AS distance
                FROM knowledge_base
                WHERE embedding_vector IS NOT NULL
                ORDER BY distance
                LIMIT $2
                """,
                emb.tolist(), limit
            )
            results = [dict(r) for r in rows]

            if len(results) < limit:
                more = limit - len(results)
                tsq = " & ".join(w for w in q.split() if len(w) > 2)
                if tsq:
                    text_rows = await conn.fetch(
                        """
                        SELECT id, title, content, category, tags
                        FROM knowledge_base
                        WHERE to_tsvector('arabic', content) @@ to_tsquery('arabic', $1)
                           OR LOWER(content) LIKE '%' || LOWER($2) || '%'
                        ORDER BY ts_rank(
                            to_tsvector('arabic', content),
                            to_tsquery('arabic', $1)
                        ) DESC
                        LIMIT $3
                        """,
                        tsq, q, more
                    )
                    seen = {r["id"] for r in results}
                    for r in text_rows:
                        if r["id"] not in seen:
                            results.append(dict(r))

            return results

    @staticmethod
    async def add_item(title, content, category=None, tags=None):
        t = KnowledgeBase._sanitize_input(title)
        c = KnowledgeBase._sanitize_input(content)
        cat = KnowledgeBase._sanitize_input(category) if category else None
        emb = await current_app.embedding_service.get_embedding(c)

        async with current_app.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO knowledge_base
                   (title, content, category, tags, embedding_vector)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                t, c, cat, tags or [], emb.tolist()
            )
            return row["id"]

    @staticmethod
    async def update_item(item_id, title=None, content=None, category=None, tags=None):
        parts, params = [], [item_id]
        idx = 2
        if title is not None:
            tt = KnowledgeBase._sanitize_input(title)
            parts.append(f"title=${idx}"); params.append(tt); idx += 1
        if content is not None:
            cc = KnowledgeBase._sanitize_input(content)
            parts.append(f"content=${idx}"); params.append(cc); idx += 1
            emb = await current_app.embedding_service.get_embedding(cc)
            parts.append(f"embedding_vector=${idx}"); params.append(emb.tolist()); idx += 1
        if category is not None:
            cc = KnowledgeBase._sanitize_input(category)
            parts.append(f"category=${idx}"); params.append(cc); idx += 1
        if tags is not None:
            parts.append(f"tags=${idx}"); params.append(tags); idx += 1
        if not parts:
            return False
        parts.append("updated_at=CURRENT_TIMESTAMP")
        clause = ", ".join(parts)

        async with current_app.db_pool.acquire() as conn:
            await conn.execute(
                f"UPDATE knowledge_base SET {clause} WHERE id=$1",
                *params
            )
        return True

    @staticmethod
    async def delete_item(item_id):
        async with current_app.db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM knowledge_base WHERE id=$1",
                item_id
            )
        return True

    @staticmethod
    async def rebuild_embeddings():
        async with current_app.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content FROM knowledge_base
                WHERE embedding_vector IS NULL
                   OR updated_at > embedding_updated_at
                """
            )
            cnt = 0
            for r in rows:
                emb = await current_app.embedding_service.get_embedding(r["content"])
                await conn.execute(
                    """
                    UPDATE knowledge_base
                    SET embedding_vector=$1,
                        embedding_updated_at=CURRENT_TIMESTAMP
                    WHERE id=$2
                    """,
                    emb.tolist(), r["id"]
                )
                cnt += 1
            return cnt

    @staticmethod
    def _sanitize_input(text):
        if not text:
            return text
        return re.sub(r"[\'\"\\;]", "", text)

    @staticmethod
    def _sanitize_query(q):
        if not q:
            return ""
        qq = re.sub(r"[\'\"\\;]", "", q)
        stop = {'من','إلى','في','على','عن','مع','هل','كيف','لماذا','متى','أين','ماذا','و','أو','ثم','لكن'}
        ws = [w for w in qq.split() if w.lower() not in stop]
        return " ".join(ws) if ws else qq
