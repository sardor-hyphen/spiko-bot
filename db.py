import asyncio
import logging
from typing import AsyncGenerator
from urllib.parse import urlparse, quote_plus

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Assuming your config is imported here
from bot.config import config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- URL Sanitization for Aiven / Asyncpg ---
def get_cleaned_db_url(raw_url: str) -> str:
    """
    Standardizes the URL for async use and encodes passwords to handle 
    special characters common in Aiven (like #, @, or :).
    """
    if not raw_url:
        logger.error("DATABASE_URL is not set in environment!")
        return "sqlite+aiosqlite:///bot.db" # Fallback

    raw_url = raw_url.strip()

    # 1. SQLAlchemy async requires the +asyncpg driver prefix
    if raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif not raw_url.startswith("postgresql+asyncpg://"):
        # If no protocol at all, assume it's meant to be async postgres
        raw_url = "postgresql+asyncpg://" + raw_url.split("://")[-1]

    try:
        # 2. Extract and encode the password to handle symbols like # or @
        parsed = urlparse(raw_url)
        if parsed.password:
            safe_password = quote_plus(parsed.password)
            # Reconstruct the URL with the safe password
            # We use replace carefully to only hit the password section
            raw_url = raw_url.replace(f":{parsed.password}@", f":{safe_password}@", 1)

        # 3. Add SSL requirement (required by Aiven)
        # asyncpg uses 'ssl=True' (or 'ssl=require')
        if "ssl=" not in raw_url:
            separator = "&" if "?" in raw_url else "?"
            raw_url += f"{separator}ssl=True"

    except Exception as e:
        logger.error(f"Error while cleaning Database URL: {e}")
    
    return raw_url

# Generate the cleaned URL for the engine
ASYNC_DATABASE_URL = get_cleaned_db_url(config.DATABASE_URL)

# --- Robust Engine Configuration ---
engine = create_async_engine(
    ASYNC_DATABASE_URL, 
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True # Checks connection health before every query
)

AsyncSessionLocal = async_sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((OperationalError, OSError, asyncio.TimeoutError)),
    reraise=True
)
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a database session with automatic retry logic for connection issues.
    Ensures rollback on error and close on exit.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except SQLAlchemyError as e:
            logger.error(f"Database session error: {e}")
            await session.rollback()
            raise
        finally:
            await session.close()

async def check_db_health() -> bool:
    """
    Performs a simple query to verify database connectivity.
    Includes retry logic and better error handling.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with AsyncSessionLocal() as session:
                # Use a simple query that tests connectivity
                result = await session.execute(text("SELECT 1 as test"))
                test_value = result.scalar()
                if test_value == 1:
                    logger.info(f"Database health check passed (attempt {attempt + 1})")
                    return True
        except Exception as e:
            logger.warning(f"Database health check failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                logger.error(f"Database health check failed after {max_retries} attempts")
                return False
            # Wait before retrying
            await asyncio.sleep(1)
    
    return False