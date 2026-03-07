FROM python:3.10-slim

WORKDIR /app

# Install system dependencies (for potential build tools or pg_config)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install dependencies into the system python environment
# Add --user is not needed in docker unless running as non-root, but here we run as root by default in slim
# However, sometimes uvicorn isn't found if installed in a weird way.
# Let's ensure pip install puts binaries in a known location or call via python -m
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code into /app/bot to maintain package structure
COPY . /app/bot

# Set PYTHONPATH so 'import bot.main' works
ENV PYTHONPATH=/app

# Expose port (Render sets PORT env var)
EXPOSE 8000

# Run the application using 'python -m uvicorn' to avoid PATH issues
CMD ["sh", "-c", "python -m uvicorn bot.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
