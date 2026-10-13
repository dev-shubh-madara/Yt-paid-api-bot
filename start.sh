#!/bin/sh
# Starts the PO-token generator in the background (set POT_SERVER=false to skip it), then the API.
# If the generator cannot start, the API still runs; yt-dlp then falls back to its own methods.
if [ "${POT_SERVER:-true}" != "false" ] && [ -f /home/app/bgutil-ytdlp-pot-provider/server/build/main.js ]; then
  (while true; do
     node /home/app/bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 || true
     sleep 5
   done) &
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
