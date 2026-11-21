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
ENV PYTHONUNBUFFERED=1 PORT=8100
EXPOSE 8100
CMD ["python", "-m", "autotrade.main"]
