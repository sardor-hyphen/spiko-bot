# Spiko Telegram Bot

This folder contains the Telegram Bot service for Spiko. It is built using `python-telegram-bot` (async) and `FastAPI` (for webhooks and notification endpoints).

## Directory Structure

- `main.py`: Entry point for the bot service (FastAPI application + Telegram Bot).
- `handlers.py`: Contains all bot command handlers, callbacks, and interaction logic.
- `models.py`: SQLAlchemy async models (mirrors backend schema).
- `db.py`: Database connection and session management.
- `config.py`: Configuration loading.
- `utils.py`: Helper functions (e.g., rate limiting).
- `create_tables.py`: Script to create database tables.
- `Dockerfile`: Deployment configuration for Render/Docker.

## Local Setup

1. **Create Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Variables**:
   Create a `.env` file in this directory or set environment variables:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token
   DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db_name
   WEB_APP_URL=https://your-app-url.onrender.com
   SECRET_KEY=your_secret_key
   ```
   *Note: For local dev, you can use `ngrok` for webhook URL or just polling (modify `main.py` to use `bot_app.run_polling()` instead of webhook).*

4. **Run Locally**:
   ```bash
   uvicorn bot.main:app --reload
   ```

## Production Deployment Checklist (Render)

1. **Dockerfile**: Ensure `Dockerfile` is present in the root of the bot directory.
2. **Environment Variables**: Set the following in Render dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `DATABASE_URL` (Use Internal Connection string if possible, ensure it starts with `postgres://` or `postgresql://`. The code handles the `asyncpg` scheme conversion automatically.)
   - `WEB_APP_URL` (The public URL of your deployed service)
   - `SECRET_KEY`
3. **Webhook URL**: The bot automatically sets the webhook on startup to `{WEB_APP_URL}/api/webhook/telegram`. Ensure this URL is accessible.
4. **Secret Token Rotation**: Ideally, implement a secret token check in the webhook handler for added security (optional for MVP).

## Architecture Diagram

```mermaid
graph TD
    User((User)) -->|Commands/Callbacks| TG[Telegram API]
    TG -->|Webhook Update| Bot[Bot Service (FastAPI)]
    Bot -->|Async Queries| DB[(PostgreSQL)]
    Backend[Main Backend] -->|Trigger Notification| Bot
    Bot -->|Send Message| TG
    User -->|Open WebApp| Frontend[Frontend React App]
    Frontend -->|Auth via TG| Backend
```

## Notification System

The bot exposes endpoints for the backend to trigger notifications:

- **POST /api/notify/student/assignment**: Notify a student about a new assignment.
- **POST /api/notify/teacher/submission**: Notify a teacher about a student submission.

Example Payload:
```json
{
  "student_telegram_id": "123456789",
  "title": "Module 1",
  "due_date": "2023-12-31"
}
```
