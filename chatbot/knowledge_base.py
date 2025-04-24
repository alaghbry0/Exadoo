# knowledge_base.py
import re
import asyncio
from urllib.parse import urlparse
import time
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
import logging
from langchain.text_splitter import RecursiveCharacterTextSplitter

# تهيئة Logger لهذا الملف
logger = logging.getLogger(__name__)

# تهيئة المقسم النصي
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    length_function=len,
    is_separator_regex=False
)


class KnowledgeBase:
    def __init__(self, app=None):
        self.app = None
        self.logger = logging.getLogger(__name__)
        self.db_pool = None
        self.embedding_service = None
        self.ai_service = None

        if app:
            self.init_app(app)

    def init_app(self, app):
        """تهيئة مع التطبيق"""
        try:
            self.app = app
            self.db_pool = app.db_pool
            self.embedding_service = app.embedding_service
            # تأكد من تعيين ai_service بشكل صحيح
            if hasattr(app, 'ai_service'):
                self.ai_service = app.ai_service
            elif hasattr(app, 'ai_manager'):
                self.ai_service = app.ai_manager
            self.logger.info("✅ KnowledgeBase initialized with app context")
        except AttributeError as e:
            self.logger.critical(f"Failed to initialize KnowledgeBase: {str(e)}")
            raise


    async def search(self, query: str, limit: int = 5, k_rrf: int = 60, debug=False):
        """
        Performs hybrid search using Vector (Chunks), Text (FTS+Snippets+Tags),
        and URL search, then merges results using Reciprocal Rank Fusion (RRF).
        """
        start_time = time.time()

        # 1. Optimize Query
        optimized_query, keywords_list = await self.optimize_search_query(query)
        if debug:
            self.logger.info(f"Original query: {query}")
            self.logger.info(f"Optimized query for search: {optimized_query}")
            self.logger.info(f"Keywords for tag/text search: {keywords_list}")

        q = optimized_query
        if not q: return []

        # 2. Extract URLs
        urls = self._extract_urls(query)
        has_url = len(urls) > 0

        # 3. Execute Searches in Parallel
        search_tasks = [
            self._vector_search(q, limit * 3),  # جلب نتائج أكثر للدمج
            self._text_search(q, keywords_list, limit * 3),  # جلب نتائج أكثر للدمج
        ]
        if has_url:
            search_tasks.append(self._url_search(urls, limit))

        results_lists = await asyncio.gather(*search_tasks)
        vector_results = results_lists[0]
        text_results = results_lists[1]
        url_results = results_lists[2] if has_url else []

        if debug:
            self.logger.info(f"Vector search raw results: {len(vector_results)}")
            self.logger.info(f"Text search raw results: {len(text_results)}")
            self.logger.info(f"URL search raw results: {len(url_results)}")

        # 4. Apply Reciprocal Rank Fusion (RRF)
        rrf_scores = defaultdict(float)
        doc_details = {}  # لتخزين تفاصيل المستند (العنوان، أفضل مقتطف، إلخ)

        # RRF constant
        k = k_rrf

        # Process Vector Results (Chunk-based)
        for i, r in enumerate(vector_results):
            kb_id = r['kb_id']
            rank = i + 1
            rrf_scores[kb_id] += 1.0 / (k + rank)
            if kb_id not in doc_details:
                doc_details[kb_id] = {'title': r['title'], 'category': r['category'], 'tags': r['tags']}
            # تخزين أفضل (أقرب) جزء كنص أساسي مرشح
            if 'best_chunk' not in doc_details[kb_id] or r['distance'] < doc_details[kb_id].get('best_chunk_distance',
                                                                                                float('inf')):
                doc_details[kb_id]['best_chunk'] = r['chunk_text']
                doc_details[kb_id]['best_chunk_distance'] = r['distance']

        # Process Text Results (FTS, Snippets, Tags)
        for i, r in enumerate(text_results):
            kb_id = r['kb_id']  # تأكدنا من إرجاع kb_id
            rank = i + 1
            rrf_scores[kb_id] += 1.0 / (k + rank)
            if kb_id not in doc_details:
                doc_details[kb_id] = {'title': r['title'], 'category': r['category'], 'tags': r['tags']}
            # تخزين المقتطف إذا وجد (له أولوية أعلى من الجزء)
            if r.get('snippet'):
                doc_details[kb_id]['snippet'] = r['snippet']
            # تحديث العنوان/الفئة/الوسوم إذا لم تكن موجودة من البحث الدلالي
            if 'title' not in doc_details[kb_id]: doc_details[kb_id]['title'] = r['title']
            if 'category' not in doc_details[kb_id]: doc_details[kb_id]['category'] = r['category']
            if 'tags' not in doc_details[kb_id]: doc_details[kb_id]['tags'] = r['tags']

        # Process URL Results
        for i, r in enumerate(url_results):
            kb_id = r['kb_id']  # تأكدنا من إرجاع kb_id
            rank = i + 1
            rrf_scores[kb_id] += 1.0 / (k + rank)  # نعطي URL score أيضاً
            if kb_id not in doc_details:
                doc_details[kb_id] = {'title': r['title'], 'category': r['category'], 'tags': r['tags']}
            doc_details[kb_id]['url_match'] = True  # إضافة علامة تطابق الرابط

        # 5. Combine details and RRF scores
        final_results_data = []
        for kb_id, score in rrf_scores.items():
            details = doc_details.get(kb_id, {})
            # اختيار أفضل نص لعرضه: المقتطف أولاً، ثم أفضل جزء، ثم العنوان كحل أخير
            display_text = details.get('snippet', details.get('best_chunk', details.get('title')))
            final_results_data.append({
                "id": kb_id,  # أو kb_id
                "title": details.get('title', 'N/A'),
                "snippet": display_text,  # إعادة تسمية الحقل ليكون موحداً
                "category": details.get('category'),
                "tags": details.get('tags'),
                "score": score,  # نقاط RRF النهائية
                "debug_info": {  # معلومات إضافية للـ debugging (اختياري)
                    "has_snippet": 'snippet' in details,
                    "has_chunk": 'best_chunk' in details,
                    "url_match": details.get('url_match', False)
                }
            })

        # 6. Sort by RRF score and limit
        final_results_data.sort(key=lambda x: x['score'], reverse=True)
        final_results = final_results_data[:limit]

        if debug:
            end_time = time.time()
            self.logger.info(f"Hybrid search with RRF completed in {end_time - start_time:.3f} seconds")
            self.logger.info(f"Returning {len(final_results)} results after RRF merging.")

        return final_results

    async def optimize_search_query(self, user_query: str) -> Tuple[str, List[str]]:
        """
        Optimize search query using Chat Prefix Completion.
        Extracts keywords, cleans them, and returns both a space-separated
        query string and a list of unique keywords.
        """
        if not user_query or len(user_query.strip()) < 3:
            return user_query, []

        try:
            # تعديل هنا: التعامل مع ai_service مباشرة بدلاً من محاولة الوصول إلى models
            # يبدو أن self.ai_service هو فعلاً كائن DeepSeekService وليس AIModelManager
            deepseek_service = self.ai_service
            if not deepseek_service or not hasattr(deepseek_service, 'get_chat_prefix_completion'):
                self.logger.warning("DeepSeek service or prefix completion not available")
                return user_query, []

            messages_for_prefix = [
                {"role": "user",
                 "content": f"استخرج الكلمات المفتاحية الرئيسية للبحث من الاستعلام التالي: \"{user_query}\""},
                {"role": "assistant", "content": "\n- ", "prefix": True}
            ]

            response = await deepseek_service.get_chat_prefix_completion(
                messages=messages_for_prefix,
                settings={"temperature": 0.1, "max_tokens": 100, "stop": ["\n\n"]}
            )

            if not response: return user_query, []

            completion_content = response.get("content", "")
            keywords = []
            for line in completion_content.split("\n"):
                keyword_part = ""
                if line.strip().startswith("-"):
                    keyword_part = line.strip("- ").strip()
                elif line.strip():
                    keyword_part = line.strip()

                if keyword_part and len(keyword_part) > 1:
                    cleaned_keyword = keyword_part.replace("**", "")
                    cleaned_keyword = re.sub(r'\s*\([^)]*\)', '', cleaned_keyword).strip()
                    cleaned_keyword = cleaned_keyword.strip("()")
                    cleaned_keyword = re.sub(r'\s+', ' ', cleaned_keyword).strip()

                    if cleaned_keyword and len(cleaned_keyword) > 1:
                        sub_keywords = [kw.strip() for kw in cleaned_keyword.split('|') if kw.strip()]
                        keywords.extend(sub_keywords)

            seen = set()
            unique_keywords = []
            for k in keywords:
                if k not in seen:
                    seen.add(k)
                    unique_keywords.append(k)

            if unique_keywords:
                optimized_string = " ".join(unique_keywords)
                optimized_string = optimized_string.replace('|', ' ')
                optimized_string = re.sub(r'\s+', ' ', optimized_string).strip()
                return optimized_string, unique_keywords
            else:
                return user_query, []

        except Exception as e:
            self.logger.error(f"Error optimizing search query: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return user_query, []

    async def _vector_search(self, query: str, limit: int):
        """
        Perform vector-based semantic search on content chunks.
        Returns relevant chunks along with parent document info.
        """
        if not query: return []
        try:
            query_embedding = await self.embedding_service.get_embedding(query)
            async with self.db_pool.acquire() as conn:
                # البحث في جدول الأجزاء knowledge_chunks
                # والانضمام إلى knowledge_base لجلب العنوان والمعلومات الأخرى
                # استخدام البحث التقريبي الأقرب (ANN) باستخدام <->
                rows = await conn.fetch(
                    """
                    SELECT
                        c.kb_id,          -- ID المستند الأصلي
                        c.chunk_text,     -- نص الجزء ذو الصلة
                        c.chunk_order,    -- ترتيب الجزء (مفيد للفرز أو السياق)
                        c.embedding_vector <-> $1 AS distance, -- حساب المسافة
                        b.title,          -- عنوان المستند الأصلي
                        b.category,       -- فئة المستند الأصلي
                        b.tags            -- وسوم المستند الأصلي
                    FROM knowledge_chunks c
                    JOIN knowledge_base b ON c.kb_id = b.id -- الانضمام لجلب معلومات المستند
                    ORDER BY distance ASC -- ترتيب حسب الأقرب (أقل مسافة)
                    LIMIT $2 * 3; -- جلب نتائج أكثر مبدئياً لمعالجتها لاحقاً (مثل تجميع حسب kb_id)
                    """,
                    query_embedding.tolist(), limit
                )

                # --- معالجة النتائج (تجميع حسب kb_id واختيار الأفضل) ---
                # هذا يضمن عدم تكرار نفس المستند الأصلي عدة مرات بسبب تطابق عدة أجزاء
                best_chunks_per_doc = {}
                for r in rows:
                    kb_id = r['kb_id']
                    # نحتفظ فقط بالجزء الأفضل (الأقرب) لكل مستند
                    if kb_id not in best_chunks_per_doc or r['distance'] < best_chunks_per_doc[kb_id]['distance']:
                        # تحويل الصف إلى قاموس قابل للتعديل
                        result_dict = dict(r)
                        # تحويل المسافة إلى نقاط لتتوافق مع الطرق الأخرى (اختياري هنا، يمكن فعله في RRF)
                        result_dict['vector_score'] = 1.0 - min(result_dict['distance'], 1.0)
                        best_chunks_per_doc[kb_id] = result_dict

                # تحويل القاموس إلى قائمة وفرزها حسب المسافة مرة أخرى (أو النقاط)
                # وتطبيق الحد النهائي limit
                sorted_best_chunks = sorted(best_chunks_per_doc.values(), key=lambda x: x['distance'])
                final_vector_results = sorted_best_chunks[:limit]

                # إرجاع النتائج النهائية للبحث الدلالي
                # كل عنصر يمثل أفضل جزء تم العثور عليه لمستند معين
                return final_vector_results

        except Exception as e:
            self.logger.error(f"Error in vector chunk search: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return []

    async def _text_search(self, query: str, keywords: List[str], limit: int):
        """
        Perform enhanced text-based search using FTS, snippets, and tag matching.
        Uses pre-calculated tsvector columns and ts_headline.
        """
        if not query and not keywords:
            return []

        try:
            # تحضير استعلام tsquery (يمكن تحسينه باستخدام websearch_to_tsquery إذا لزم الأمر)
            # استخدام الكلمات من query المفصولة بـ |
            tsq_string = " | ".join(filter(None, query.split()))
            if not tsq_string:  # في حالة كان query فارغاً بعد التقسيم
                if keywords:  # استخدام الكلمات المفتاحية كبديل
                    tsq_string = " | ".join(keywords)
                else:
                    return []  # لا يوجد ما نبحث به

            # تحضير قائمة الكلمات المفتاحية للبحث في الوسوم
            # التأكد من أن القائمة ليست فارغة وأن عناصرها نصوص
            tags_array = [str(k) for k in keywords if k and isinstance(k, str)]
            if not tags_array:
                tags_array = ["___NO_VALID_KEYWORDS___"]  # قيمة غير محتملة

            async with self.db_pool.acquire() as conn:
                # استخدام الأعمدة المفهرسة title_tsv, content_tsv
                # استخدام ts_headline لاستخراج المقتطفات من content
                # استخدام ts_rank لتقييم الصلة
                # البحث في tags باستخدام @> (يحتوي على)
                sql_query = """
                        WITH ranked_results AS (
                            SELECT
                                id,
                                title,
                                content, -- لا نزال بحاجة إليه لـ ts_headline
                                category,
                                tags,
                                -- حساب الرتبة بناءً على تطابق العنوان والمحتوى مع الاستعلام
                                ts_rank(title_tsv || content_tsv, to_tsquery('arabic', $1)) AS base_rank,
                                 -- إضافة وزن للكلمات في العنوان
                                ts_rank(title_tsv, to_tsquery('arabic', $1)) * 0.2 AS title_boost,
                                -- التحقق من تطابق الوسوم
                                (tags @> $2::text[]) AS tag_match -- $2 قائمة الكلمات للوسوم
                            FROM knowledge_base
                            -- الشرط الرئيسي: تطابق العنوان أو المحتوى أو الوسوم
                            WHERE (title_tsv || content_tsv) @@ to_tsquery('arabic', $1)
                               OR (tags @> $2::text[]) -- البحث في الوسوم
                        )
                        SELECT
                            r.id,
                            r.title,
                            r.category,
                            r.tags,
                            -- حساب الرتبة النهائية
                            (r.base_rank + r.title_boost + (CASE WHEN r.tag_match THEN 0.3 ELSE 0 END)) AS rank, -- تعزيز نقاط تطابق الوسوم والعنوان
                            -- إنشاء المقتطف من المحتوى الأصلي
                            ts_headline('arabic', r.content, to_tsquery('arabic', $1),
                                        'HighlightAll=TRUE, MinWords=15, MaxWords=45, ShortWord=3, FragmentDelimiter=" ... "') AS snippet
                        FROM ranked_results r
                        ORDER BY rank DESC
                        LIMIT $3;
                        """
                rows = await conn.fetch(
                    sql_query,
                    tsq_string,  # $1: الاستعلام النصي لـ tsquery
                    tags_array,  # $2: قائمة الكلمات المفتاحية للبحث في tags
                    limit  # $3: الحد الأقصى للنتائج
                )
                # إرجاع id المستند الأصلي (kb_id) لتتوافق مع مخرجات vector_search
                return [{"kb_id": r["id"], **dict(r)} for r in rows]  # إضافة kb_id
        except Exception as e:
            self.logger.error(f"Error in text search: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return []

    async def _url_search(self, urls, limit):
        """Search for content containing specific URLs"""
        if not urls:
            return []
        try:
            url_patterns = [f"%{url}%" for url in urls]
            # بناء الاستعلام ديناميكياً
            where_clauses = " OR ".join([f"content LIKE ${i + 1}" for i in range(len(url_patterns))])
            query = f"""
                    SELECT id as kb_id, title, content, category, tags, TRUE as url_match
                    FROM knowledge_base
                    WHERE {where_clauses}
                    LIMIT ${len(url_patterns) + 1}
                """
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(query, *url_patterns, limit)
                return [dict(r) for r in rows]
        except Exception as e:
            self.logger.error(f"Error in URL search: {str(e)}")
            return []



    @staticmethod
    def _extract_urls(text: str) -> List[str]:
        """Extract URLs from text"""
        return re.findall(r"https?://[\w./-]+", text)






    @staticmethod
    async def add_item(title: str, content: str, category: Optional[str] = None, tags: Optional[List[str]] = None):
        """
        Adds a new item to knowledge_base and its content chunks to knowledge_chunks.
        Uses new splitter and potentially batch embedding if service supports it.
        """
        t = KnowledgeBase._sanitize_input(title)
        c = KnowledgeBase._sanitize_input(content)  # المحتوى الأصلي الكامل
        cat = KnowledgeBase._sanitize_input(category) if category else None
        tags_list = tags or []

        async with current_app.db_pool.acquire() as conn:
            async with conn.transaction():
                # 1. حساب تضمين للمستند ككل (اختياري، للعنوان مثلاً)
                # يمكنك إزالة هذا إذا لم تعد تستخدم embedding_vector في knowledge_base
                doc_embedding_vec = await current_app.embedding_service.get_embedding(t)  # تضمين العنوان

                # 2. إضافة السجل الرئيسي إلى knowledge_base
                kb_row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_base (title, content, category, tags, embedding_vector)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id;
                    """,
                    t, c, cat, tags_list, doc_embedding_vec.tolist() if doc_embedding_vec is not None else None
                )
                if not kb_row:
                    raise Exception("Failed to insert into knowledge_base")
                kb_id = kb_row['id']

                # 3. تقسيم المحتوى إلى أجزاء باستخدام المقسم الجديد
                chunks = split_text_into_chunks_langchain(c)

                if not chunks:
                    current_app.logger.warning(
                        f"No chunks generated for kb_id {kb_id}. Content might be empty or too short.")
                    return kb_id  # لا يزال يتم إضافة السجل الرئيسي

                # 4. حساب التضمينات للأجزاء وإضافتها إلى knowledge_chunks
                # Check if the embedding service has a batch method
                if hasattr(current_app.embedding_service, 'get_embeddings_batch'):
                    current_app.logger.debug(f"Using batch embedding for {len(chunks)} chunks for kb_id {kb_id}")
                    chunk_embeddings = await current_app.embedding_service.get_embeddings_batch(chunks)
                    # تأكد من أن عدد التضمينات يطابق عدد الأجزاء
                    if len(chunk_embeddings) != len(chunks):
                        current_app.logger.error(
                            f"Mismatch in chunk count ({len(chunks)}) and embedding count ({len(chunk_embeddings)}) for kb_id {kb_id}. Falling back to individual calculation.")
                        # Fallback to individual calculation if batch fails
                        chunk_embeddings = []
                        for chunk_text in chunks:
                            emb = await current_app.embedding_service.get_embedding(chunk_text)
                            chunk_embeddings.append(emb)
                else:
                    current_app.logger.debug(
                        f"Using individual embedding calculation for {len(chunks)} chunks for kb_id {kb_id}")
                    embedding_tasks = [current_app.embedding_service.get_embedding(chunk) for chunk in chunks]
                    chunk_embeddings = await asyncio.gather(*embedding_tasks)

                # إعداد البيانات للإدراج المجمع (إذا كان مدعومًا بكفاءة)
                # asyncpg's executemany is generally efficient
                chunk_data_to_insert = []
                for i, (chunk_text, chunk_embedding) in enumerate(zip(chunks, chunk_embeddings)):
                    if chunk_embedding is not None:
                        chunk_data_to_insert.append((kb_id, chunk_text, i, chunk_embedding.tolist()))
                    else:
                        current_app.logger.warning(f"Could not generate embedding for chunk {i} of kb_id {kb_id}")

                if chunk_data_to_insert:
                    await conn.executemany(
                        """
                        INSERT INTO knowledge_chunks (kb_id, chunk_text, chunk_order, embedding_vector)
                        VALUES ($1, $2, $3, $4);
                        """,
                        chunk_data_to_insert
                    )

                current_app.logger.info(f"Added item {kb_id} with {len(chunk_data_to_insert)} chunks.")
                return kb_id

    # --- تعديل update_item ---
    @staticmethod
    async def update_item(item_id: int, title: Optional[str] = None, content: Optional[str] = None,
                          category: Optional[str] = None, tags: Optional[List[str]] = None):
        """
        Updates an existing item in knowledge_base and rebuilds its chunks if content changed.
        Uses new splitter and batch embeddings.
        """
        updates = {}
        params = []
        param_idx = 1
        content_changed = False
        new_content = None
        new_title = None  # لتضمين المستند الاختياري

        # بناء جملة التحديث لـ knowledge_base
        if title is not None:
            t = KnowledgeBase._sanitize_input(title)
            updates["title"] = f"${param_idx}"
            params.append(t)
            param_idx += 1
            new_title = t
        if content is not None:
            c = KnowledgeBase._sanitize_input(content)
            updates["content"] = f"${param_idx}"
            params.append(c)
            param_idx += 1
            content_changed = True
            new_content = c
        if category is not None:
            cat = KnowledgeBase._sanitize_input(category)
            updates["category"] = f"${param_idx}"
            params.append(cat)
            param_idx += 1
        if tags is not None:
            updates["tags"] = f"${param_idx}"
            params.append(tags)
            param_idx += 1

        if not updates and not content_changed:  # لا حاجة للتحديث إذا لم يتغير شيء أساسي
            current_app.logger.info(f"No fields to update for item {item_id}.")
            return True  # يعتبر ناجحًا لأنه لا يوجد شيء للتغيير

        # إضافة تحديث لـ updated_at (إذا لم يتم تلقائياً بواسطة trigger)
        # ولـ embedding_updated_at في knowledge_base إذا لزم الأمر
        updates["updated_at"] = "CURRENT_TIMESTAMP"
        # لا نحدث embedding_updated_at هنا، لأنها قد تشير إلى تضمين المستند الكامل

        async with current_app.db_pool.acquire() as conn:
            async with conn.transaction():
                # 1. جلب البيانات الحالية إذا لزم الأمر
                current_data = await conn.fetchrow("SELECT title, content FROM knowledge_base WHERE id = $1 FOR UPDATE",
                                                   item_id)
                if not current_data:
                    current_app.logger.warning(f"Item {item_id} not found for update.")
                    return False

                # تحديد العنوان النهائي لتضمين المستند (إذا كنت لا تزال تستخدمه)
                final_title = new_title if new_title is not None else current_data['title']

                # 2. تحديث تضمين المستند ككل (اختياري)
                if "embedding_vector" in updates:  # Check if embedding_vector column exists/is needed
                    doc_embedding = await current_app.embedding_service.get_embedding(final_title)
                    updates["embedding_vector"] = f"${param_idx}"
                    params.append(doc_embedding.tolist() if doc_embedding is not None else None)
                    param_idx += 1
                    updates["embedding_updated_at"] = "CURRENT_TIMESTAMP"  # تحديث وقت التضمين الرئيسي

                # 3. بناء وتنفيذ استعلام التحديث لـ knowledge_base
                set_clause = ", ".join([f"{k} = {v}" for k, v in updates.items()])
                update_kb_query = f"UPDATE knowledge_base SET {set_clause} WHERE id = ${param_idx} RETURNING content"  # لا نحتاج لـ id، فقط content إذا لم يتم تحديثه
                params.append(item_id)
                updated_kb_row = await conn.fetchrow(update_kb_query, *params)

                if not updated_kb_row:
                    # هذا لا يجب أن يحدث بسبب FOR UPDATE ولكن كإجراء احترازي
                    current_app.logger.error(f"Failed to update knowledge_base for item {item_id} after lock.")
                    raise Exception(f"Failed to update knowledge_base for item {item_id}")  # لإلغاء الـ transaction

                # 4. إعادة بناء الأجزاء إذا تغير المحتوى
                if content_changed:
                    current_app.logger.info(f"Content changed for item {item_id}. Rebuilding chunks...")
                    final_content = new_content  # تم التأكد من أنه ليس None لأنه content_changed=True

                    # أ. حذف الأجزاء القديمة
                    await conn.execute("DELETE FROM knowledge_chunks WHERE kb_id = $1", item_id)

                    # ب. تقسيم المحتوى الجديد وحساب وإضافة الأجزاء الجديدة
                    chunks = split_text_into_chunks_langchain(final_content)
                    if chunks:
                        # حساب التضمينات (استخدام batch إذا متاح)
                        if hasattr(current_app.embedding_service, 'get_embeddings_batch'):
                            chunk_embeddings = await current_app.embedding_service.get_embeddings_batch(chunks)
                            if len(chunk_embeddings) != len(chunks):
                                current_app.logger.error(
                                    f"Chunk/embedding count mismatch during update for kb_id {item_id}. Aborting chunk rebuild.")
                                # يمكنك اختيار الاستمرار بالتضمين الفردي أو إيقاف العملية
                                raise Exception("Chunk/embedding count mismatch during update.")
                        else:
                            embedding_tasks = [current_app.embedding_service.get_embedding(chunk) for chunk in chunks]
                            chunk_embeddings = await asyncio.gather(*embedding_tasks)

                        # إعداد البيانات للإدراج المجمع
                        chunk_data_to_insert = []
                        for i, (chunk_text, chunk_embedding) in enumerate(zip(chunks, chunk_embeddings)):
                            if chunk_embedding is not None:
                                chunk_data_to_insert.append((item_id, chunk_text, i, chunk_embedding.tolist()))
                            else:
                                current_app.logger.warning(
                                    f"Could not generate embedding for chunk {i} of kb_id {item_id} during update.")

                        if chunk_data_to_insert:
                            await conn.executemany(
                                """
                                INSERT INTO knowledge_chunks (kb_id, chunk_text, chunk_order, embedding_vector)
                                VALUES ($1, $2, $3, $4);
                                """,
                                chunk_data_to_insert
                            )
                            current_app.logger.info(f"Rebuilt {len(chunk_data_to_insert)} chunks for item {item_id}.")
                    else:
                        current_app.logger.info(f"No chunks generated after update for kb_id {item_id}.")

                return True  # تم التحديث بنجاح



    # --- تعديل delete_item ---
    # لا حاجة لتعديل الكود هنا إذا تم استخدام ON DELETE CASCADE في تعريف الجدول knowledge_chunks
    # قاعدة البيانات ستحذف الأجزاء تلقائياً عند حذف السجل الرئيسي.
    # إذا لم تستخدم CASCADE، ستحتاج لإضافة:
    # await conn.execute("DELETE FROM knowledge_chunks WHERE kb_id=$1", item_id)
    # قبل حذف السجل من knowledge_base.
    @staticmethod
    async def delete_item(item_id: int):
        """Deletes an item from knowledge_base and its associated chunks (relies on ON DELETE CASCADE)."""
        try:
            async with current_app.db_pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM knowledge_base WHERE id=$1",
                    item_id
                )
                # result عادة يكون بصيغة "DELETE N" حيث N هو عدد الصفوف المحذوفة
                deleted_count = int(result.split()[-1])
                if deleted_count > 0:
                    current_app.logger.info(f"Deleted item {item_id} and its associated chunks (via CASCADE).")
                    return True
                else:
                    current_app.logger.warning(f"Attempted to delete item {item_id}, but it was not found.")
                    return False
        except Exception as e:
            current_app.logger.error(f"Error deleting item {item_id}: {str(e)}")
            return False

    @staticmethod
    async def batch_search(queries: List[str], limit_per_query: int = 3) -> Dict[str, List[Dict]]:
        """
        Perform batch search for multiple queries at once
        Returns a dictionary mapping each query to its results
        """
        results = {}
        tasks = []

        for query in queries:
            if query and isinstance(query, str):
                tasks.append(KnowledgeBase.search(query, limit=limit_per_query))

        if tasks:
            batch_results = await asyncio.gather(*tasks)

            for i, query in enumerate(queries):
                if i < len(batch_results):
                    results[query] = batch_results[i]

        return results


    @staticmethod
    def _preprocess_query(q):
        """
        Preprocess query by:
        - Cleaning special characters
        - Removing Arabic stop words
        - Preserving URLs and important elements
        """
        if not q: return ""
        qq = re.sub(r"[\'\"\\;]", "", q)
        stop = {'من', 'إلى', 'في', 'على', 'عن', 'مع', 'هل', 'كيف', 'لماذا', 'متى', 'أين', 'ماذا', 'و', 'أو', 'ثم',
                'لكن', 'هذا', 'هذه', 'ذلك', 'تلك', 'الذي', 'التي', 'الذين', 'اللذين', 'اللتين', 'هم', 'هن', 'نحن', 'هو',
                'هي', 'انت', 'انتم', 'انتن', 'انا'}
        special_tokens = KnowledgeBase._extract_urls(q)
        special_tokens_placeholder = "___URL___"
        for url in special_tokens: qq = qq.replace(url, special_tokens_placeholder)
        words = []
        for w in qq.split():
            if w == special_tokens_placeholder:
                words.append(special_tokens.pop(0) if special_tokens else w)
            elif w.lower() not in stop:
                words.append(w)
        return " ".join(words) if words else qq

    @staticmethod
    def _sanitize_input(text):
        """Sanitize input to prevent injection attacks"""
        if not text:
            return ""

        # Basic sanitization
        sanitized = re.sub(r"[\'\"\\;]", "", text)
        return sanitized

    @staticmethod
    async def rebuild_embeddings(kb_ids: Optional[List[int]] = None):
        """
        Rebuilds embeddings for chunks in the knowledge_chunks table.
        If kb_ids is provided, only rebuilds for those specific knowledge base items.
        Otherwise, rebuilds for ALL items.
        WARNING: Rebuilding all can be time-consuming and resource-intensive.
        """
        start_time = time.time()
        total_chunks_processed = 0
        total_kb_items_processed = 0

        # تحديد الاستعلام بناءً على ما إذا تم توفير kb_ids
        if kb_ids:
            query = f"SELECT id, content FROM knowledge_base WHERE id = ANY($1::int[])"
            params = (kb_ids,)
            current_app.logger.info(f"Starting rebuild_embeddings for {len(kb_ids)} specific items...")
        else:
            query = "SELECT id, content FROM knowledge_base"
            params = ()
            current_app.logger.info("Starting rebuild_embeddings for ALL items...")

        # حجم الدفعة للمعالجة (عدد سجلات knowledge_base في كل مرة)
        # اضبط هذا الرقم بناءً على ذاكرة الخادم وقدرة خدمة التضمين
        kb_batch_size = 50

        async with current_app.db_pool.acquire() as conn:
            # استخدام cursor للمعالجة التدريجية إذا كانت مجموعة البيانات كبيرة جدًا (متقدم)
            # للطريقة الأبسط، سنقوم بجلب دفعات من المعرفات والمحتوى
            # Fetch all relevant kb items first (consider memory for very large KB)
            try:
                all_kb_items = await conn.fetch(query, *params)
            except Exception as e:
                current_app.logger.error(f"Error fetching knowledge base items for rebuild: {e}")
                return 0  # Or raise

            total_kb_items = len(all_kb_items)
            current_app.logger.info(f"Found {total_kb_items} knowledge base items to process.")

            for i in range(0, total_kb_items, kb_batch_size):
                batch_kb_items = all_kb_items[i:i + kb_batch_size]
                current_app.logger.info(
                    f"Processing KB batch {i // kb_batch_size + 1}/{(total_kb_items + kb_batch_size - 1) // kb_batch_size}...")

                # تجميع كل الأجزاء من هذه الدفعة من سجلات knowledge_base
                all_chunks_in_batch = []  # List of tuples: (kb_id, chunk_text, chunk_order)
                kb_ids_in_batch = []  # لتتبع المعرفات لحذف الأجزاء القديمة

                for kb_item in batch_kb_items:
                    kb_id = kb_item['id']
                    content = kb_item['content']
                    kb_ids_in_batch.append(kb_id)

                    if not content:
                        current_app.logger.warning(f"Skipping kb_id {kb_id} due to empty content.")
                        continue

                    chunks = split_text_into_chunks_langchain(content)
                    for order, chunk_text in enumerate(chunks):
                        if chunk_text and chunk_text.strip():  # التأكد من أن الجزء غير فارغ
                            all_chunks_in_batch.append((kb_id, chunk_text, order))

                if not all_chunks_in_batch:
                    current_app.logger.info(f"No processable chunks found in this KB batch (IDs: {kb_ids_in_batch}).")
                    # تأكد من حذف الأجزاء القديمة لهذه المعرفات الفارغة
                    async with conn.transaction():
                        await conn.execute(f"DELETE FROM knowledge_chunks WHERE kb_id = ANY($1::int[])",
                                           kb_ids_in_batch)
                    total_kb_items_processed += len(batch_kb_items)
                    continue

                # استخراج نصوص الأجزاء لحساب التضمينات المجمعة
                chunk_texts_for_embedding = [c[1] for c in all_chunks_in_batch]

                # حساب التضمينات دفعة واحدة
                try:
                    start_embed_time = time.time()
                    if hasattr(current_app.embedding_service, 'get_embeddings_batch'):
                        chunk_embeddings = await current_app.embedding_service.get_embeddings_batch(
                            chunk_texts_for_embedding)
                    else:  # Fallback to individual (less efficient)
                        tasks = [current_app.embedding_service.get_embedding(text) for text in
                                 chunk_texts_for_embedding]
                        chunk_embeddings = await asyncio.gather(*tasks)
                    embed_time = time.time() - start_embed_time
                    current_app.logger.info(
                        f"Calculated {len(chunk_embeddings)} embeddings for batch in {embed_time:.2f} seconds.")

                    if len(chunk_embeddings) != len(all_chunks_in_batch):
                        current_app.logger.error(
                            f"Embedding count mismatch ({len(chunk_embeddings)}) vs chunk count ({len(all_chunks_in_batch)}) in batch processing. Skipping DB operations for this batch.")
                        # يمكنك إضافة منطق أكثر تفصيلاً هنا لمعالجة الخطأ
                        continue

                except Exception as e:
                    current_app.logger.error(f"Error during batch embedding calculation: {e}")
                    # يمكنك اختيار إيقاف العملية أو تخطي هذه الدفعة
                    continue  # تخطي هذه الدفعة والانتقال إلى التالية

                # إعداد البيانات للإدراج
                chunk_data_to_insert = []
                for idx, (kb_id, chunk_text, chunk_order) in enumerate(all_chunks_in_batch):
                    embedding_vector = chunk_embeddings[idx]
                    if embedding_vector is not None:
                        chunk_data_to_insert.append((kb_id, chunk_text, chunk_order, embedding_vector.tolist()))
                    else:
                        current_app.logger.warning(
                            f"Skipping chunk {chunk_order} for kb_id {kb_id} due to null embedding.")

                # تنفيذ عمليات قاعدة البيانات داخل transaction
                if chunk_data_to_insert:
                    try:
                        async with conn.transaction():
                            # 1. حذف الأجزاء القديمة لـ kb_ids في هذه الدفعة
                            await conn.execute(
                                f"DELETE FROM knowledge_chunks WHERE kb_id = ANY($1::int[])",
                                kb_ids_in_batch
                            )
                            # 2. إدراج الأجزاء الجديدة
                            await conn.executemany(
                                """
                                INSERT INTO knowledge_chunks (kb_id, chunk_text, chunk_order, embedding_vector)
                                VALUES ($1, $2, $3, $4);
                                """,
                                chunk_data_to_insert
                            )
                        total_chunks_processed += len(chunk_data_to_insert)
                        current_app.logger.info(
                            f"Successfully processed batch: Deleted old chunks and inserted {len(chunk_data_to_insert)} new chunks for {len(kb_ids_in_batch)} KB items.")
                    except Exception as e:
                        current_app.logger.error(
                            f"Database transaction failed for batch (KB IDs: {kb_ids_in_batch}): {e}")
                        # فشل الـ transaction يعني عدم حدوث أي تغييرات لهذه الدفعة
                else:
                    # إذا لم يتم إنشاء بيانات للإدراج (ربما بسبب أخطاء التضمين)
                    # لا يزال يتعين حذف الأجزاء القديمة
                    try:
                        async with conn.transaction():
                            await conn.execute(f"DELETE FROM knowledge_chunks WHERE kb_id = ANY($1::int[])",
                                               kb_ids_in_batch)
                        current_app.logger.info(
                            f"Deleted old chunks for KB IDs {kb_ids_in_batch} as no new chunks were generated/embedded.")
                    except Exception as e:
                        current_app.logger.error(
                            f"Database transaction failed while deleting old chunks for empty batch (KB IDs: {kb_ids_in_batch}): {e}")

                total_kb_items_processed += len(batch_kb_items)
                # يمكنك إضافة انتظار بسيط هنا إذا لزم الأمر لتجنب الضغط الزائد
                # await asyncio.sleep(0.1)

        end_time = time.time()
        total_time = end_time - start_time
        current_app.logger.info(
            f"Finished rebuild_embeddings. Processed {total_kb_items_processed}/{total_kb_items} KB items, generated and inserted {total_chunks_processed} chunks in {total_time:.2f} seconds.")
        return total_chunks_processed

knowledge_base = KnowledgeBase()


def split_text_into_chunks_langchain(text: str) -> List[str]:
    """يقسم النص باستخدام Langchain RecursiveCharacterTextSplitter."""
    if not text:
        return []
    return text_splitter.split_text(text)
