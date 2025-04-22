# embedding_service.py

import os
import asyncio
import torch
import numpy as np
from sentence_transformers import SentenceTransformer
from quart import current_app
# from functools import lru_cache # lru_cache غير متوافق مع async
from async_lru import alru_cache
from typing import List, Optional





class ImprovedEmbeddingService:
    def __init__(self):
        self.model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        self.local_path = "./models/paraphrase-multilingual-MiniLM-L12-v2" # تأكد أن المسار صحيح وقابل للكتابة
        self.model = None
        self.embedding_size = 384
        # تحديد الجهاز (GPU إذا كان متاحًا)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        # يمكنك جعل حجم الدفعة قابلاً للتكوين
        self.batch_size = 64 # حجم الدفعة الداخلية لـ sentence-transformer (اضبطه بناءً على ذاكرة GPU/CPU)
        current_app.logger.info(f"EmbeddingService will use device: {self.device}")

    async def initialize(self):
        """Initializes the SentenceTransformer model."""
        # لا حاجة للتنزيل اليدوي، SentenceTransformer يقوم بذلك
        try:
            # تحقق من وجود مسار النماذج، قم بإنشائه إذا لم يكن موجودًا
            if not os.path.exists(self.local_path):
                 os.makedirs(self.local_path, exist_ok=True)
                 current_app.logger.info(f"Created models directory: {self.local_path}")

            # حدد cache_folder لتوجيه التنزيل إذا لزم الأمر
            self.model = SentenceTransformer(self.model_name, device=self.device, cache_folder=self.local_path)
            # اختبار بسيط للتأكد من أن النموذج يعمل
            _ = self.model.encode("test")
            current_app.logger.info(
                f"Model {self.model_name} initialized successfully on {self.device}"
            )
        except Exception as e:
            current_app.logger.error(f"Failed to initialize SentenceTransformer model: {e}", exc_info=True)
            # يمكنك رفع الخطأ لمنع بدء تشغيل التطبيق بدون نموذج
            raise

    @alru_cache(maxsize=2048) # زيادة حجم الكاش قد يكون مفيدًا
    async def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """Gets embedding for a single text string, using cache."""
        if not self.model:
            current_app.logger.error("Embedding model not initialized.")
            return None # أو ارفع استثناءً

        if not text or not text.strip():
            # Return None or a zero vector based on how you handle empty inputs downstream
            # Returning None might be safer to explicitly handle missing embeddings
            return None
            # return np.zeros(self.embedding_size, dtype=np.float32)

        # لا حاجة لمعالجة النص المسبقة هنا، sentence-transformer يتعامل معها
        # التشغيل في executor لتجنب حظر الحلقة الرئيسية
        loop = asyncio.get_event_loop()
        try:
             # استخدام lambda لتمرير الوسائط بشكل صحيح إلى run_in_executor
             emb = await loop.run_in_executor(
                 None, # استخدام المنفذ الافتراضي (ThreadPoolExecutor)
                 lambda txt: self.model.encode(txt, convert_to_numpy=True),
                 text # تمرير النص كوسيط لـ lambda
             )
             # تحويل إلى float32 إذا لم يكن كذلك بالفعل (عادةً ما يكون كذلك)
             return emb.astype(np.float32)
        except Exception as e:
             current_app.logger.error(f"Error generating embedding for text: '{text[:100]}...': {e}")
             return None # إرجاع None عند حدوث خطأ

    async def get_embeddings_batch(self, texts: List[str]) -> List[Optional[np.ndarray]]:
        """Gets embeddings for a batch of texts efficiently."""
        if not self.model:
            current_app.logger.error("Embedding model not initialized.")
            return [None] * len(texts) # أو ارفع استثناءً

        if not texts:
            return []

        # تنظيف النصوص: استبدال النصوص الفارغة أو التي تحتوي على مسافات فقط بـ None مؤقتًا
        # لمنع النموذج من معالجتها وإرجاع مؤشر لوجود خطأ محتمل
        processed_texts = [text if text and text.strip() else None for text in texts]
        texts_to_embed = [text for text in processed_texts if text is not None]

        results = [None] * len(texts) # قائمة النتائج النهائية بنفس حجم الإدخال

        if not texts_to_embed:
             current_app.logger.warning("Received a batch with only empty texts.")
             return results # إرجاع قائمة من None

        loop = asyncio.get_event_loop()
        try:
            current_app.logger.debug(f"Starting batch embedding for {len(texts_to_embed)} non-empty texts...")
            # تشغيل عملية التضمين المجمعة في executor
            # مرر batch_size و show_progress_bar إذا كنت تعالج كميات كبيرة جدًا
            embeddings = await loop.run_in_executor(
                None,
                lambda txt_list: self.model.encode(
                    txt_list,
                    convert_to_numpy=True,
                    batch_size=self.batch_size, # التحكم في حجم الدفعة الداخلية
                    show_progress_bar=False # يمكن تفعيله للعمليات الطويلة جدًا في السجلات
                ),
                texts_to_embed
            )
            current_app.logger.debug(f"Finished batch embedding. Got {len(embeddings)} vectors.")

            # إعادة دمج النتائج في القائمة الأصلية بالحجم الصحيح
            embed_iter = iter(embeddings)
            for i, text in enumerate(processed_texts):
                if text is not None:
                    # التأكد من الحصول على النوع الصحيح
                    results[i] = next(embed_iter).astype(np.float32)
                # النصوص التي كانت None ستبقى None في results

            return results

        except Exception as e:
            current_app.logger.error(f"Error generating batch embeddings: {e}", exc_info=True)
            # في حالة الخطأ، قم بإرجاع قائمة من None بحجم الإدخال الأصلي
            return [None] * len(texts)

    async def get_similarity(self, t1: str, t2: str) -> Optional[float]:
        """Calculates cosine similarity between two texts."""
        # Use the cached single embedding method
        e1_task = self.get_embedding(t1)
        e2_task = self.get_embedding(t2)
        e1, e2 = await asyncio.gather(e1_task, e2_task)

        if e1 is None or e2 is None:
             current_app.logger.warning(f"Cannot calculate similarity due to missing embedding for '{t1[:20]}...' or '{t2[:20]}...'")
             return None # أو 0.0 إذا كان ذلك مناسبًا أكثر لسياقك

        # حساب التشابه (cosine similarity)
        norm1 = np.linalg.norm(e1)
        norm2 = np.linalg.norm(e2)
        if norm1 > 0 and norm2 > 0:
            similarity = np.dot(e1, e2) / (norm1 * norm2)
            # التأكد من أن القيمة ضمن النطاق [-1, 1] بسبب الأخطاء الرقمية المحتملة
            return float(np.clip(similarity, -1.0, 1.0))
        else:
            return 0.0 # إذا كان أحد المتجهات صفريًا