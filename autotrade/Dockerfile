# Autotrade service image (needs Chromium for the login capture, runs alongside GBot on the VPS).
# Build from the repository root:  docker build -f autotrade/Dockerfile -t autotrade .
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY autotrade/requirements.txt autotrade/requirements.txt
RUN pip install --no-cache-dir -r autotrade/requirements.txt \
 && python -m playwright install chromium
COPY autotrade autotrade
COPY data data
COPY strategy strategy
COPY config.py runtime_env.py strategy.json ./
# Bound driver heap and allocator fragmentation without requiring single-process Chromium.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AUTOTRADE_PORT=8100 AUTOTRADE_HOST=0.0.0.0 NODE_OPTIONS="--max-old-space-size=256" \
    MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072
EXPOSE 8100
CMD ["python", "-m", "autotrade.main"]
