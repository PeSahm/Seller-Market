# Dockerfile for Seller-Market Trading Bot
# Multi-stage build for smaller image size

FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Ensure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY *.py ./
COPY config.example.ini ./

# Create directories for runtime data
RUN mkdir -p logs order_results

# Environment variables with defaults
# OCR_SERVICE_URL: Override this when running in Docker Compose
ENV OCR_SERVICE_URL=http://localhost:8080
ENV PYTHONUNBUFFERED=1

# Health check - verify Python can import main modules
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import simple_config_bot; print('OK')" || exit 1

# Default command - runs Telegram bot with integrated scheduler
# This is what run_bot.bat does: python simple_config_bot.py
CMD ["python", "simple_config_bot.py"]
