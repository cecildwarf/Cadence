# Cadence — python:3.12-slim + ffmpeg
FROM python:3.12-slim

# Timezone is configurable (TZ env var, default UTC) — set it in
# .env / docker-compose.yml to match your location. tzdata is needed
# so ZoneInfo can resolve named timezones like America/New_York.
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC

# PUID/PGID are build args so the files the container writes are
# owned by the same user your media server runs as (default 1000:1000;
# override with --build-arg or via docker-compose.yml).
ARG PUID=1000
ARG PGID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g "$PGID" cadence 2>/dev/null || true \
 && useradd -u "$PUID" -g "$PGID" -m cadence 2>/dev/null || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Let the app user write to /app (Python bytecode cache, and any
# temp files if DATA_DIR is ever pointed elsewhere).
RUN chown -R "$PUID":"$PGID" /app

# Runs as UID/GID 1000 by default (override with PUID/PGID build args
# in docker-compose.yml) so files it writes are owned by the same user
# your media server runs as.
USER ${PUID}:${PGID}

EXPOSE 8285
CMD ["python", "app.py"]
