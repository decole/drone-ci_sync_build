FROM python:3.12-slim

# Install git and ca-certificates for git ls-remote
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app/ ./app/
COPY main.py .

# Storage directory for SQLite
RUN mkdir -p /app/data
ENV DATA_DIR="/app/data"
ENV DATABASE_URL="sqlite:////app/data/drone_sync.db"

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
