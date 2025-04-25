@payment_confirmation_bp.before_app_serving
async def startup():
    logging.info("🚀 بدء تهيئة وحدة تأكيد المدفوعات...")
    timeout = 120  # ⏳ 120 ثانية
    start_time = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout:
            logging.error(f"""
            ❌ فشل حرج بعد {timeout} ثانية:
            - db_pool موجود؟ {hasattr(current_app, 'db_pool')}

            """)
            raise RuntimeError("فشل التهيئة")

        if hasattr(current_app, 'db_pool') and current_app.db_pool is not None:
            try:
                async with current_app.db_pool.acquire() as conn:
                    await conn.execute("SELECT 1")
                logging.info("✅ اتصال قاعدة البيانات فعّال")
                break
            except Exception as e:
                logging.warning(f"⚠️ فشل التحقق من اتصال قاعدة البيانات: {str(e)}")
                await asyncio.sleep(5)
        else:
            logging.info(f"⏳ انتظار db_pool... ({elapsed:.1f}/{timeout} ثانية)")
            await asyncio.sleep(5)

    logging.info("🚦 بدء المهام الخلفية...")
    asyncio.create_task(periodic_check_payments())

