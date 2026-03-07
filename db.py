import asyncio
import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from bot.config import config
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Robust Engine Configuration ---
# Pool settings for high load:
# pool_size: number of permanent connections
# max_overflow: number of additional connections allowed beyond pool_size
# pool_timeout: seconds to wait for a connection before giving up
# pool_recycle: seconds after which a connection is closed and re-established (prevents stale connections)
engine = create_async_engine(
    config.DATABASE_URL, 
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True # Vital for checking connection health before use
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
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
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False
