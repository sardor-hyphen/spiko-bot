
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from bot.db import Base
from bot.models import User
from bot.config import config

# Use an in-memory SQLite DB for tests to be fast and isolated
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture(scope="session")
def engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    yield engine
    engine.sync_engine.dispose()

@pytest.fixture(scope="function")
async def db_session(engine):
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Create session
    async_session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session
        await session.rollback() # Rollback after each test
        
    # Drop tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.fixture
def mock_update(mocker):
    update = mocker.MagicMock()
    update.effective_user.id = 123456
    update.effective_user.first_name = "TestUser"
    update.effective_user.username = "testuser"
    update.message.reply_text = mocker.AsyncMock()
    update.callback_query.answer = mocker.AsyncMock()
    update.callback_query.edit_message_text = mocker.AsyncMock()
    return update

@pytest.fixture
def mock_context(mocker):
    context = mocker.MagicMock()
    context.bot.send_message = mocker.AsyncMock()
    return context
