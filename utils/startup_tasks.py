import logging
import asyncpg
from typing import Optional

# نحصل على المسجل (Logger)
logger = logging.getLogger(__name__)


async def mark_stale_tasks_as_failed(db_pool: Optional[asyncpg.Pool]):
    """
    Finds any tasks that were left in 'in_progress' or 'RUNNING' state
    and marks them as failed, assuming a server restart occurred.
    This function should be called once on application startup.
    """
    if not db_pool:
        logger.error("Database pool is not available. Skipping stale task check.")
        return

    logger.info("Checking for stale background tasks from a previous run...")

    try:
        async with db_pool.acquire() as conn:
            # 1. تحديث المهام العامة العالقة في messaging_batches
            # نستخدم jsonb_build_object لإنشاء كائن JSON بشكل آمن
            updated_batches_result = await conn.fetch(
                """
                UPDATE messaging_batches
                SET 
                    status = 'failed', 
                    completed_at = NOW(), 
                    error_details = jsonb_build_object(
                        'error_message', 'Task failed due to server restart',
                        'error_key', 'server_restart'
                    )
                WHERE status = 'in_progress'
                RETURNING batch_id
                """
            )
            if updated_batches_result:
                logger.warning(f"Marked {len(updated_batches_result)} stale messaging batches as FAILED.")

            # 2. تحديث عمليات فحص القنوات العالقة في channel_audits
            updated_audits_result = await conn.fetch(
                """
                UPDATE channel_audits
                SET 
                    status = 'FAILED', 
                    completed_at = NOW(), 
                    error_message = 'Audit failed due to server restart'
                WHERE status = 'RUNNING'
                RETURNING id
                """
            )
            if updated_audits_result:
                logger.warning(f"Marked {len(updated_audits_result)} stale channel audits as FAILED.")

    except Exception as e:
        logger.error(f"Could not mark stale tasks as failed: {e}", exc_info=True)