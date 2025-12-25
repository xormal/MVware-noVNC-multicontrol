FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY config/ ./config/

# Copy entrypoint script
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# Create directories for logs and screenshots
RUN mkdir -p logs screenshots

# Expose ports
# 5001: Flask API
# 8765: WebSocket Proxy
EXPOSE 5001 8765

# Environment variables (override with docker-compose or -e flags)
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV FLASK_DEBUG=false

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5001/api/v1/health', timeout=5)" || exit 1

# Use entrypoint script to start both services
ENTRYPOINT ["/app/docker-entrypoint.sh"]
