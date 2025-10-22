# syntax=docker/dockerfile:1.7
FROM python:3.14-slim

# Needed to talk to Docker over /var/run/docker.sock
RUN pip install --no-cache-dir docker pytz

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py /app/app.py
RUN chmod +x /app/app.py

# Default envs (override at runtime)
ENV SMTP_HOST=mail \
    SMTP_PORT=25 \
    SMTP_FROM=docker-watcher@localhost \
    SMTP_TO=root@localhost \
    SMTP_TLS=0 \
    SMTP_USER= \
    SMTP_PASS= \
    RESTARTS_IN_WINDOW=3 \
    RESTART_WINDOW_SEC=60 \
    BACKOFF_BASE_SEC=60 \
    BACKOFF_MAX_SEC=3600 \
    INCLUDE_RECOVERY=1 \
    INCLUDE_IMAGE=1 \
    CHECK_PING_EVERY=10 \
    TZ=UTC

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import docker; docker.from_env().ping()"

CMD ["/app/app.py"]
