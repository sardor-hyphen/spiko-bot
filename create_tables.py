import asyncio
import logging
from sqlalchemy import text
from bot.db import engine, Base
# Import all models to ensure they are registered with Base.metadata
from bot.models import (
    User, Task, TaskAssignment, TaskModule, AssessmentScore, 
    SessionUsage, FeedbackSummary, OneTimeToken, UserSubscription, PaymentTransaction
)
from bot.config import config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def create_tables():
    logger.info("Starting database table creation...")
    
    # Mask the password in the DB URL for logging
    db_url = config.DATABASE_URL
    if "@" in db_url:
        start = db_url.find("://") + 3
        end = db_url.find("@")
        masked_url = db_url[:start] + "****" + db_url[end:]
    else:
        masked_url = db_url
        
    logger.info(f"Connecting to database: {masked_url}")

    try:
        async with engine.begin() as conn:
            # Optional: Test connection first
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection successful.")
            
            # Create tables
            await conn.run_sync(Base.metadata.create_all)
            
        logger.info("Tables created successfully!")
        
    except Exception as e:
        logger.error(f"Error creating tables: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(create_tables())
