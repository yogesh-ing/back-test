# Forward Testing Simulator — Docker image (Step 20)
# Build: docker build -t forward-test .
# Run: docker run -d --env-file .env -v $(pwd)/state:/app/state forward-test

FROM python:3.11-slim

# System deps for psycopg2 and tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create state dir
RUN mkdir -p state

# Env
ENV PYTHONPATH=/app/src
ENV TZ=Asia/Kolkata

# Health check: ensure engine can import
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "from backtest.forward.engine import ForwardTestingEngine; print('ok')" || exit 1

# Default command runs the engine with default config
CMD ["python", "-m", "backtest.forward.engine"]

# Expose no ports by default; dashboard (Step 19) would expose 8501
# EXPOSE 8501
