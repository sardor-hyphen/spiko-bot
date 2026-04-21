import os
import uvicorn
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
# Ensure CommandHandler is imported for the custom handlers
from telegram.ext import ApplicationBuilder, CommandHandler

# Local imports
from bot.config import config
from bot.handlers import setup_handlers
from bot.score_handler import score_handler
from bot.class_handler import class_handler
from bot.db import engine, Base, check_db_health
from bot.utils import retry_async

# ==============================================================================
# 1. LOGGING CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 2. BOT INITIALIZATION (Must happen before calling handlers)
# ==============================================================================
# Initialize bot application
bot_app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

# Setup standard handlers (from handlers.py)
setup_handlers(bot_app)

# Setup custom command handlers
bot_app.add_handler(CommandHandler("score", score_handler))
bot_app.add_handler(CommandHandler("class", class_handler))

# ==============================================================================
# 3. LIFESPAN MANAGEMENT (Startup & Shutdown)
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Starting up Spiko Bot service...")
    
    # 1. Database Connection & Migration
    try:
        logger.info("Verifying database connection health...")
        db_healthy = await check_db_health()
        if not db_healthy:
             logger.error("Initial database health check failed! Attempting to proceed...")
        
        logger.info("Syncing database schema (ensure_runtime_schema)...")
        async with engine.begin() as conn:
            # This creates tables if they don't exist. Does NOT overwrite data.
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema sync complete.")
    except Exception as e:
        logger.critical(f"Startup failed during database initialization: {e}")
        raise e

    # 2. Telegram Bot Initialization
    try:
        logger.info("Initializing Telegram Application...")
        await bot_app.initialize()
        await bot_app.start()
        logger.info("Telegram Application started.")
    except Exception as e:
        logger.critical(f"Failed to initialize Telegram bot: {e}")
        raise e
    
    # 3. Webhook Setup
    webhook_url = f"{config.BOT_SERVER_URL}/api/webhook/telegram"
    logger.info(f"Setting webhook to: {webhook_url}")
    
    async def set_webhook():
        await bot_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        
    try:
        await retry_async(set_webhook, retries=5)
        logger.info("Webhook successfully set.")
    except Exception as e:
        logger.error(f"Failed to set webhook after retries: {e}")
        raise e

    yield # --- App is running and serving requests ---
    
    # --- Shutdown ---
    logger.info("Shutting down Spiko Bot service...")
    try:
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("Telegram Bot stopped.")
    except Exception as e:
        logger.error(f"Error during bot shutdown: {e}")
    
    try:
        await engine.dispose()
        logger.info("Database engine connections closed.")
    except Exception as e:
        logger.error(f"Error disposing database engine: {e}")

# ==============================================================================
# 4. FASTAPI APPLICATION SETUP
# ==============================================================================
app = FastAPI(
    title="Spiko Bot API",
    description="Telegram bot for Spiko language learning platform",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# 5. WEBHOOK & HEALTH ENDPOINTS
# ==============================================================================
@app.get("/")
async def root():
    return {"service": "spiko-bot", "status": "running", "version": "1.0.0"}

@app.get("/ping")
async def ping():
    return {"status": "pong"}

@app.post("/api/webhook/telegram")
async def telegram_webhook(request: Request):
    """Handle incoming updates from Telegram."""
    try:
        data = await request.json()
        logger.info(f"Received webhook update: {data}")
        update = Update.de_json(data, bot_app.bot)
        # Pass the update to the python-telegram-bot logic
        await bot_app.process_update(update)
        logger.info("Update processed successfully")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        # We return 200 even on error so Telegram doesn't keep resending a broken update
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health_check():
    """Full health check including Database and Bot status."""
    result = {
        "status": "healthy",
        "timestamp": "2026-03-23T07:25:00Z"
    }
    try:
        db_ok = await check_db_health()
        result["database"] = "connected" if db_ok else "disconnected"
        if not db_ok: result["status"] = "degraded"
        
        result["bot"] = "connected" if bot_app.bot else "disconnected"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    return result

@app.get("/health-lite")
async def health_check_lite():
    return {"status": "healthy", "app": "running"}

# ==============================================================================
# 6. NOTIFICATION ENDPOINTS (External triggers)
# ==============================================================================
@app.post("/api/notify/student/assignment")
async def notify_student_assignment(request: Request):
    try:
        data = await request.json()
        chat_id = data.get("student_telegram_id")
        title = data.get("title")
        due_date = data.get("due_date")
        assignment_id = data.get("assignment_id")

        if not chat_id:
            raise HTTPException(status_code=400, detail="Missing student_telegram_id")

        msg = (
            f"🔔 **New Assignment Published!**\n\n"
            f"📝 Title: {title}\n"
            f"📅 Due: {due_date}\n\n"
            f"Choose your action below:"
        )

        # Import needed functions
        from bot.shared_utils import generate_webapp_url, get_user_by_telegram_id
        from bot.db import get_db_session

        # Get user and generate webapp URL
        async for session in get_db_session():
            user = await get_user_by_telegram_id(session, chat_id)
            if user:
                webapp_url = generate_webapp_url(user, assignment_id)
                keyboard = [
                    [InlineKeyboardButton("📝 View Details", web_app=WebAppInfo(url=webapp_url))],
                    [InlineKeyboardButton("✅ Mark Started", callback_data=f"assignment_started_{assignment_id}")],
                    [InlineKeyboardButton("⏰ Remind Later", callback_data=f"assignment_remind_{assignment_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', reply_markup=reply_markup)
            else:
                # Fallback if user not found
                await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        return {"status": "sent"}
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

        msg = (
            f"📨 **New Submission Received**\n\n"
            f"👤 Student: {student_name}\n"
            f"📝 Task: {title}\n"
            f"🕒 Time: {submitted_at}\n\n"
            f"Check the dashboard or use /start to view progress."
        )
        
        await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        return {"status": "sent"}
    except Exception as e:
        logger.error(f"Notification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# 7. EXECUTION
# ==============================================================================
if __name__ == "__main__":
    # Get port from environment (Render requirement)
    port = int(os.environ.get("PORT", 8000))
    # Note: Using the string "bot.main:app" allows for proper reload/worker management
    uvicorn.run("bot.main:app", host="0.0.0.0", port=port, reload=False)