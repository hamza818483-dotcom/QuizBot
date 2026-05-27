"""Retry Handler - Auto retry failed operations"""
import asyncio
import logging

logger = logging.getLogger(__name__)

async def retry_process_page(process_func, page_num, img_bytes, max_retries=3):
    """Process a page with retry logic"""
    for attempt in range(max_retries):
        try:
            result = await process_func(img_bytes)
            if result:
                return result
            logger.warning(f"Page {page_num} attempt {attempt+1}: no results")
        except Exception as e:
            logger.error(f"Page {page_num} attempt {attempt+1} failed: {e}")
        if attempt < max_retries - 1:
            await asyncio.sleep(2)
    logger.error(f"Page {page_num} failed after {max_retries} attempts")
    return []
