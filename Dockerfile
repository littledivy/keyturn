FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data \
    PORT=5051

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

VOLUME ["/data"]
EXPOSE 5051

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --fail http://127.0.0.1:5051/health || exit 1

CMD ["./start.sh"]
