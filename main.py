import os
import uvicorn
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import ApplicationBuilder

from bot.config import config
from bot.handlers import setup_handlers
from bot.db import engine, Base, check_db_health
from bot.utils import retry_async

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot application
bot_app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

# Setup handlers
setup_handlers(bot_app)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Starting up bot service...")
    
    # 1. Database Connection & Migration
    try:
        logger.info("Verifying database connection...")
        if not await check_db_health():
             logger.error("Initial database health check failed!")
             # We might choose to exit here or retry, but let's try to proceed 
             # as the robust engine has retries built-in.
        
        # Use existing tables if available. 
        # Base.metadata.create_all checks for existence before creating, so it's generally safe.
        # However, to be extra safe and avoid race conditions or lock issues during deployment,
        # we can wrap it in a try-except or just rely on SQLAlchemy's idempotency.
        # It DOES NOT overwrite existing data.
        logger.info("Ensuring database schema...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        logger.critical(f"Startup failed during database initialization: {e}")
        # In production, we might want to crash here to let the orchestrator restart us
        raise e

    # 2. Bot Initialization
    try:
        await bot_app.initialize()
        await bot_app.start()
    except Exception as e:
        logger.critical(f"Failed to initialize Telegram bot: {e}")
        raise e
    
    # 3. Webhook Setup
    # Use retry logic for webhook setup as network might be flaky on startup
    webhook_url = f"{config.BOT_SERVER_URL}/api/webhook/telegram"
    logger.info(f"Setting webhook to {webhook_url}")
    
    async def set_webhook():
        await bot_app.bot.set_webhook(url=webhook_url)
        
    try:
        await retry_async(set_webhook, retries=5)
    except Exception as e:
        logger.error(f"Failed to set webhook after retries: {e}")
        # Proceeding without webhook is fatal for a webhook-based bot
        raise e

    yield
    
    # --- Shutdown ---
    logger.info("Shutting down bot service...")
    try:
        # Remove webhook to prevent delivery failures while down (optional, sometimes better to leave it)
        # await bot_app.bot.delete_webhook() 
        
        await bot_app.stop()
        await bot_app.shutdown()
    except Exception as e:
        logger.error(f"Error during bot shutdown: {e}")
    
    try:
        await engine.dispose()
        logger.info("Database engine disposed.")
    except Exception as e:
        logger.error(f"Error disposing database engine: {e}")

app = FastAPI(lifespan=lifespan)

@app.post("/api/webhook/telegram")
async def telegram_webhook(request: Request):
    """Handle incoming Telegram updates."""
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        # Process update in background task or await? 
        # Awaiting is safer for now to ensure we don't drop updates if process dies,
        # but for high load, background tasks might be needed. 
        # python-telegram-bot handles updates async internally via process_update.
        await bot_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        # Return 200 OK to Telegram even on error to prevent retry loops for bad updates
        return {"status": "error", "message": "Processed with errors"}

@app.get("/health")
async def health_check():
    """
    Deep health check that verifies:
    1. App is running
    2. Database is accessible
    """
    db_status = await check_db_health()
    if not db_status:
        raise HTTPException(status_code=503, detail="Database unhealthy")
        
    return {
        "status": "healthy", 
        "database": "connected", 
        "bot": "running"
    }

# --- Notification Endpoints ---

@app.post("/api/notify/student/assignment")
async def notify_student_assignment(request: Request):
    try:
        data = await request.json()
        chat_id = data.get("student_telegram_id")
        title = data.get("title")
        due_date = data.get("due_date")
        
        if not chat_id:
            raise HTTPException(status_code=400, detail="Missing student_telegram_id")

        msg = f"🔔 **New Assignment Published!**\n\n"
        msg += f"📝 Title: {title}\n"
        msg += f"📅 Due: {due_date}\n\n"
        msg += "Tap '📝 Tasks' below to view details."
        
        # Use retry for sending messages
        async def send():
            await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            
        await retry_async(send)
        return {"status": "sent"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Notification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/notify/teacher/submission")
async def notify_teacher_submission(request: Request):
    try:
        data = await request.json()
        chat_id = data.get("teacher_telegram_id")
        student_name = data.get("student_name")
        title = data.get("title")
        submitted_at = data.get("submitted_at")
        
        if not chat_id:
             raise HTTPException(status_code=400, detail="Missing teacher_telegram_id")

        msg = f"📨 **New Submission Received**\n\n"
        msg += f"👤 Student: {student_name}\n"
        msg += f"📝 Task: {title}\n"
        msg += f"🕒 Time: {submitted_at}\n\n"
        msg += "Check the dashboard or use /start to view progress."
        
        async def send():
            await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            
        await retry_async(send)
        return {"status": "sent"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Notification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("bot.main:app", host="0.0.0.0", port=port, reload=False)
