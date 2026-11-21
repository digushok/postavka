# Серверная версия «Поставок Ozon» (OpenMediaVault / любой Linux с Docker).
# Тот же код, что и в .exe; отличается только режим запуска: OZON_SUPPLIES_SERVER=1.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static ./static
COPY fonts ./fonts

ENV OZON_SUPPLIES_SERVER=1 \
    OZON_SUPPLIES_DATA=/data \
    HOST=0.0.0.0 \
    PORT=8765 \
    TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8765/api/ping', data=b'{}'), timeout=4)" || exit 1

CMD ["python", "app.py"]
