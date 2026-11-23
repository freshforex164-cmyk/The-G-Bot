# Autotrade service image (needs Chromium for the login capture, so it is not the GBot Render service).
# Build from the repository root:  docker build -f autotrade/Dockerfile -t autotrade .
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY autotrade/requirements.txt autotrade/requirements.txt
RUN pip install --no-cache-dir -r autotrade/requirements.txt \
 && python -m playwright install chromium
COPY autotrade autotrade
COPY data data
COPY strategy strategy
COPY config.py strategy.json ./
# 512 MB Render instance: cap the Playwright driver's (Node) heap well below 4096 so it collects garbage
# early, and limit glibc malloc arenas so Python's long-running asyncio threads do not fragment memory.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8100 NODE_OPTIONS="--max-old-space-size=256" \
    MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072
EXPOSE 8100
CMD ["python", "-m", "autotrade.main"]
