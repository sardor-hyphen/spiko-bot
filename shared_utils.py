"""
Shared utility functions for bot modules
Contains functions that are used across multiple modules to avoid circular imports
"""

import jwt
from sqlalchemy import select
from bot.config import config


async def get_user_by_telegram_id(session, telegram_id):
    """Get user by telegram ID from database session."""
    from bot.models import User
    result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
    return result.scalars().first()


def generate_webapp_url(user, assignment_id: str = None) -> str:
    """
    Generates the Web App URL with a JWT token that the Backend trusts.
    Optional assignment_id can be passed to deep link to a specific task.
    """
    import datetime

    # 1. Prepare the payload (Must match what your Backend JWT logic expects)
    payload = {
        'telegram_id': user.telegram_id,
        'email': user.email, # Backend might look for this
        'is_teacher': user.is_teacher,
        'is_admin': user.is_admin,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1), # Long expiry for convenience
        'iat': datetime.datetime.utcnow()
    }

    # 2. Sign it using the Shared Secret
    token = jwt.encode(payload, config.SECRET_KEY, algorithm='HS256')

    # 3. CRITICAL: Send to root "/" NOT "/login"
    # This triggers the automatic redirection logic in App.tsx
    url = f"{config.FRONTEND_URL}/?token={token}&provider=telegram"

    # 4. Append assignment_id if provided for deep linking
    if assignment_id:
        url += f"&assignment={assignment_id}"

    return url