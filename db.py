import asyncio
import logging
import re
from typing import AsyncGenerator
from urllib.parse import urlparse, quote_plus, parse_qs, urlencode, urlunparse

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
    Standardizes the URL for async use, encodes passwords, 
    and STRIPS 'sslmode' which causes asyncpg to crash.
    """
    if not raw_url:
        logger.error("DATABASE_URL is not set in environment!")
        return "sqlite+aiosqlite:///bot.db"

    raw_url = raw_url.strip()

    # 1. Handle the Protocol/Driver
    if raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    
    # 2. Use URL Parsing to clean up parameters
    try:
        parsed = urlparse(raw_url)
        
        # Handle Password Encoding (Aiven special characters)
        password = parsed.password
        username = parsed.username
        hostname = parsed.hostname
        port = parsed.port
        path = parsed.path
        
        if password:
            safe_password = quote_plus(password)
            # Reconstruct netloc safely
            netloc = f"{username}:{safe_password}@{hostname}"
            if port:
                netloc += f":{port}"
        else:
            netloc = parsed.netloc

        # 3. CLEAN THE QUERY PARAMS (The 'sslmode' fix)
        query_params = parse_qs(parsed.query)
        
        # REMOVE 'sslmode' - this is what caused your crash
        query_params.pop('sslmode', None)
        
        # ADD 'ssl=True' - this is what Aiven/Asyncpg needs
        query_params['ssl'] = ['True']
        
        new_query = urlencode(query_params, doseq=True)

        # 4. Rebuild the final URL
        cleaned_url = urlunparse((
            "postgresql+asyncpg",
            netloc,
            path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
        
        return cleaned_url

    except Exception as e:
        logger.error(f"Critical error cleaning Database URL: {e}")
        # Final fallback: just try a manual replace if the parser fails
        return raw_url.replace("sslmode=require", "ssl=True")

# Generate the cleaned URL
ASYNC_DATABASE_URL = get_cleaned_db_url(config.DATABASE_URL)
logger.info(f"Connecting to database with cleaned async URL (password redacted)")

# --- Engine Configuration ---
engine = create_async_engine(
    ASYNC_DATABASE_URL, 
    echo=False,
    pool_size=10, # Lowered slightly to be safer on free tier
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True
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
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(text("SELECT 1 as test"))
                test_value = result.scalar()
                if test_value == 1:
                    logger.info(f"Database health check passed (attempt {attempt + 1})")
                    return True
        except Exception as e:
            logger.warning(f"Database health check failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return False
            await asyncio.sleep(1)
    return False