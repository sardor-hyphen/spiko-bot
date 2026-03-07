
import pytest
from unittest.mock import AsyncMock, MagicMock
from bot.handlers import start, role_callback
from bot.models import User

@pytest.mark.asyncio
async def test_start_new_user(mock_update, mock_context, db_session):
    # Mock the DB session
    with mock.patch("bot.handlers.get_db_session", return_value=iter([db_session])):
        await start(mock_update, mock_context)
        
        # Should reply with welcome message
        mock_update.message.reply_text.assert_called_once()
        args, _ = mock_update.message.reply_text.call_args
        assert "Welcome TestUser!" in args[0]

@pytest.mark.asyncio
async def test_role_selection(mock_update, mock_context, db_session):
    mock_update.callback_query.data = "role_teacher"
    mock_update.callback_query.from_user = mock_update.effective_user
    
    with mock.patch("bot.handlers.get_db_session", return_value=iter([db_session])):
        await role_callback(mock_update, mock_context)
        
        # Verify user created in DB
        result = await db_session.execute(select(User).where(User.telegram_id == "123456"))
        user = result.scalars().first()
        assert user is not None
        assert user.is_teacher == True
        
        mock_update.callback_query.edit_message_text.assert_called()
