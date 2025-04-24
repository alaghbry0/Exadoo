# 1. اختر صورة أساس خفيفة مع Python
FROM python:3.11-slim

# 2. تثبيت الأدوات اللازمة: git، curl، ca-certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. تثبيت Git LFS
RUN curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | bash \
    && apt-get update \
    && apt-get install -y git-lfs \
    && git lfs install \
    && rm -rf /var/lib/apt/lists/*

# 4. أنشئ مجلد العمل داخل الحاوية
WORKDIR /app

# 5. انسخ ملفات المشروع إلى الحاوية
COPY . /app

# 6. ثبت احتياجات Python
RUN pip install --no-cache-dir -r requirements.txt

# 7. اكشف المنفذ الذي سيعمل عليه التطبيق (مثلاً 5000)
EXPOSE 5000

# 8. أمر التشغيل الافتراضي
CMD ["quart", "run", "--host=0.0.0.0", "--port=5000"]
