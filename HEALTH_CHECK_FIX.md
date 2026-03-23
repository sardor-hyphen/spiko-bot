# Bot Health Check Fixes

## Issues Fixed

### 1. Enhanced Health Check Endpoint (`/health`)
- **File**: `bot/main.py`
- **Changes**:
  - Added comprehensive error handling and logging
  - Added bot connection status checking
  - Added timestamp to health check response
  - Added root endpoint (`/`) for basic service check

### 2. Improved Database Health Check
- **File**: `bot/db.py`
- **Changes**:
  - Added retry logic (3 attempts with 1-second delays)
  - Better error logging with attempt tracking
  - Enhanced query validation (checks returned value)
  - More detailed error messages

### 3. Render Configuration Updates
- **File**: `bot/render.yaml`
- **Changes**:
  - Enhanced health check configuration with proper timing
  - Added restart policy for automatic recovery
  - Added commented cron job configuration for external monitoring
  - Better health check thresholds

### 4. Dependencies
- **File**: `bot/requirements.txt`
- **Changes**:
  - Added `httpx>=0.24.0` for HTTP testing capabilities

### 5. Testing
- **File**: `bot/test_health.py`
- **Purpose**: Test script to verify health check functionality

## Health Check Endpoints

### Primary Health Check
```
GET /health
```

**Response (200 OK)**:
```json
{
  "status": "healthy",
  "database": "connected",
  "bot": "connected",
  "timestamp": "2026-03-23T06:30:00Z"
}
```

**Response (503 Service Unavailable)**:
```json
{
  "detail": "Database unhealthy"
}
```

### Basic Service Check
```
GET /
```

**Response (200 OK)**:
```json
{
  "service": "spiko-bot",
  "status": "running"
}
```

## Configuration for Render

The `render.yaml` file now includes:

```yaml
healthCheck:
  path: /health
  intervalSeconds: 30
  timeoutSeconds: 10
  unhealthyThreshold: 3
  healthyThreshold: 2
restartPolicy: on-failure
```

## Testing

Run the test script:
```bash
cd bot
python test_health.py
```

## Cron Job (Optional)

For external monitoring, you can add a cron job:

```yaml
cron:
  - command: "curl -f https://$RENDER_EXTERNAL_URL/health || exit 1"
    schedule: "*/5 * * * *"
```

This will check the health endpoint every 5 minutes and trigger a restart if it fails.

## Database Configuration

The bot expects these environment variables:
- `DATABASE_URL`: Database connection string
- `TELEGRAM_BOT_TOKEN`: Bot token
- `BOT_SERVER_URL`: Public URL for the bot service
- `SECRET_KEY`: Secret key for the application

## Common Issues and Solutions

1. **Database Connection Fails**:
   - Check `DATABASE_URL` format
   - Ensure database is accessible
   - Verify network connectivity

2. **Health Check Times Out**:
   - Increase `timeoutSeconds` in render.yaml
   - Check database performance
   - Verify server resources

3. **Bot Connection Issues**:
   - Check `TELEGRAM_BOT_TOKEN` validity
   - Verify network connectivity to Telegram API

4. **Port Configuration**:
   - Dockerfile exposes port 8000
   - render.yaml sets PORT=10000
   - Uvicorn uses ${PORT:-10000} for flexibility

## Deployment

1. Commit all changes to git
2. Push to your repository
3. Render will automatically deploy the changes
4. Monitor health through Render dashboard

## Monitoring

The health check will now:
- Check database connectivity with retries
- Verify bot application status
- Log detailed error information
- Provide clear status responses
- Support automatic restarts on failure