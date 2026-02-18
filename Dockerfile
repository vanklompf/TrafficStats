FROM python:3.12-slim

RUN . /etc/os-release && \
    for comp in non-free-firmware non-free; do \
      echo "deb http://deb.debian.org/debian $VERSION_CODENAME $comp" >> /etc/apt/sources.list.d/non-free.list; \
    done && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg curl gosu \
        intel-media-va-driver-non-free libmfx-gen1.2 && \
    rm -rf /var/lib/apt/lists/*

RUN addgroup --gid 1000 appuser && \
    adduser --disabled-password --gecos "" --uid 1000 --ingroup appuser appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /data && chown appuser:appuser /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
