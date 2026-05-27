"""ATLAS BOT - Performance Configuration"""
import asyncio

# ============================================================
# CONCURRENCY SETTINGS
# ============================================================
MAX_CONCURRENT_USERS = 100      # Max parallel users
MAX_CONCURRENT_CHANNELS = 5     # Max channels per user
POLL_DELAY = 1.5                # Seconds between polls
API_TIMEOUT = 60                # Telegram API timeout
GEMINI_TIMEOUT = 60             # Gemini API timeout

# ============================================================
# RATE LIMITER
# ============================================================
class RateLimiter:
    """Prevent flood control"""
    def __init__(self, max_per_second=20):
        self.max = max_per_second
        self.tokens = max_per_second
        self.last_refill = asyncio.get_event_loop().time()
    
    async def acquire(self):
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last_refill
        self.tokens = min(self.max, self.tokens + elapsed * self.max)
        self.last_refill = now
        if self.tokens < 1:
            await asyncio.sleep((1 - self.tokens) / self.max)
            self.tokens = 0
        else:
            self.tokens -= 1

rate_limiter = RateLimiter(max_per_second=25)

# ============================================================
# SAFETY LIMITS
# ============================================================
MAX_PDF_SIZE_MB = 20            # Max PDF size without Pyrogram
MAX_CSV_ROWS = 500             # Max rows per CSV
MAX_BATCH_SIZE = 50             # Max polls per batch

print("✅ Performance config loaded - Multi-user, Multi-channel, Balanced!")
