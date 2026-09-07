FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir ".[browser]" \
    && python -m playwright install --with-deps chromium

VOLUME ["/app/Downloaded", "/app/data"]
EXPOSE 8000

CMD ["media-collector-api", "--config", "config.example.yml", "--host", "0.0.0.0", "--port", "8000"]
