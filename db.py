import asyncio
import logging
from typing import AsyncGenerator
from urllib.parse import quote_plus

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

def get_bulletproof_url(raw_url: str) -> str:
    """
    Strips ANY protocol prefix safely, encodes the password, 
    and forces the correct asyncpg SSL format.
    """
    if not raw_url:
        logger.error("DATABASE_URL is not set!")
        return "sqlite+aiosqlite:///bot.db"

    try:
        # 1. Strip ANY protocol (fixes the "user=postgresql+asyncpg" bug)
        # This splits at '://' and takes everything after it.
        url_without_scheme = raw_url.split("://", 1)[-1]

        # 2. Split credentials from the host/db
        # rsplit('@', 1) ensures if password has '@', it breaks at the LAST one
        cred_part, host_db_part = url_without_scheme.rsplit('@', 1)

        # 3. Split username and password
        username, raw_password = cred_part.split(':', 1)

        # 4. URL-encode the password to make '#' and '@' safe
        safe_password = quote_plus(raw_password)

        # 5. Clean the host and database path (Obliterates '?sslmode=require')
        host_db_path = host_db_part.split('?')[0].split('#')[0]

        # 6. Rebuild the perfect asyncpg URL
        perfect_url = f"postgresql+asyncpg://{username}:{safe_password}@{host_db_path}?ssl=require"
        
        return perfect_url

    except Exception as e:
        logger.critical(f"Failed to parse database URL: {e}")
        # Let it crash loudly here rather than failing silently later
        raise ValueError(f"Could not parse DATABASE_URL: {e}")

# Generate the pristine URL
ASYNC_DATABASE_URL = get_bulletproof_url(config.DATABASE_URL)
logger.info("Database URL parsed successfully.")

# --- Engine Configuration ---
engine = create_async_engine(
    ASYNC_DATABASE_URL, 
    echo=False,
    pool_size=10,        
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
    """Verifies connectivity to Aiven."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(text("SELECT 1"))
                if result.scalar() == 1:
                    logger.info("Database health check passed!")
                    return True
        except Exception as e:
            logger.warning(f"Database health check failed (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    return False