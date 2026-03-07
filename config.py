import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")
    
    # Logic to fix Render's postgres:// scheme
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    # 1. The public URL of THIS Bot Service (for webhooks)
    BOT_SERVER_URL = os.getenv("BOT_SERVER_URL", "https://spiko-bot.onrender.com") 

    # 2. The URL of your React Frontend (for the "Open App" button)
    FRONTEND_URL = os.getenv("FRONTEND_URL", "https://speeko.onrender.com")

    SECRET_KEY = os.getenv("SECRET_KEY", "speeko-neural-master-key-2024")

config = Config()