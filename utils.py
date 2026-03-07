import time
import logging
import functools
from collections import defaultdict
from typing import Callable, Any
from telegram import Update
from telegram.ext import ContextTypes
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Rate Limiter ---
class RateLimiter:
    def __init__(self, limit=30, window=60):
        self.limit = limit
        self.window = window
        self.requests = defaultdict(list)

    def is_allowed(self, user_id):
        now = time.time()
        user_requests = self.requests[user_id]
        
        # Remove old requests
        while user_requests and user_requests[0] < now - self.window:
            user_requests.pop(0)
            
        if len(user_requests) < self.limit:
            user_requests.append(now)
            return True
        return False

rate_limiter = RateLimiter()

# --- Decorators ---

def rate_limit(func: Callable) -> Callable:
    """Limits user interaction frequency."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user:
            user_id = update.effective_user.id
            if not rate_limiter.is_allowed(user_id):
                logger.warning(f"Rate limit exceeded for user {user_id}")
                if update.message:
                    await update.message.reply_text("⏳ You are sending messages too quickly. Please wait a moment.")
                elif update.callback_query:
                    await update.callback_query.answer("⏳ Slow down!", show_alert=True)
                return 
        return await func(update, context, *args, **kwargs)
    return wrapper

def robust_handler(func: Callable) -> Callable:
    """
    Comprehensive error handling wrapper for bot handlers.
    Catches exceptions, logs them with context, and informs the user.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            user_id = update.effective_user.id if update.effective_user else "Unknown"
            logger.error(f"Critical error in handler {func.__name__} for user {user_id}: {e}", exc_info=True)
            
            error_msg = "⚠️ An unexpected error occurred. Our team has been notified."
            
            try:
                if update.callback_query:
                    await update.callback_query.answer(error_msg, show_alert=True)
                elif update.message:
                    await update.message.reply_text(error_msg)
            except Exception as send_err:
                logger.error(f"Failed to send error message to user: {send_err}")
            
            # Re-raise if you want it to bubble up to global error handlers, 
            # but usually handling it here is safer for the bot process.
            return None
    return wrapper

# --- Retry Helper ---
# Use this for critical external calls (e.g. specialized API calls)
async def retry_async(func, *args, retries=3, **kwargs):
    for i in range(retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if i == retries - 1:
                raise e
            wait = 2 ** i
            logger.warning(f"Operation failed, retrying in {wait}s: {e}")
            await asyncio.sleep(wait)
