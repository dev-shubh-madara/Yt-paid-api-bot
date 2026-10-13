# ── stage 1: the YouTube "PO token" generator (bgutil). It lets yt-dlp pass YouTube's bot check without cookies. ──
FROM node:22-slim AS pot
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG POT_VERSION=2.0.2
RUN git clone --depth 1 --branch ${POT_VERSION} https://github.com/Brainicism/bgutil-ytdlp-pot-provider /pot \
    && cd /pot/server && npm ci && npx tsc && npm prune --omit=dev

# ── stage 2: the API ──
FROM python:3.12-slim

# yt-dlp needs a JavaScript runtime to solve YouTube's signature challenges.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY start.sh ./start.sh
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN useradd -m app && mkdir -p /data && chown app /data && chmod +x /app/start.sh
# yt-dlp's plugin also finds the generator here on its own (slower script mode) if the background server is not running
COPY --from=pot --chown=app /pot/server /home/app/bgutil-ytdlp-pot-provider/server
USER app
EXPOSE 8000
# ONE worker on purpose: rate limiter is in-memory and the DB is SQLite.
CMD ["sh", "/app/start.sh"]
