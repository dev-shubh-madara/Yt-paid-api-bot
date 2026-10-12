FROM python:3.12-slim

# yt-dlp needs a JavaScript runtime to solve YouTube's signature challenges.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
RUN useradd -m app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
# ONE worker on purpose: rate limiter is in-memory and the DB is SQLite.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
