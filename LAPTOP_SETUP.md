# Run the API on your own laptop (home IP, free)

A home internet connection is a residential IP, so YouTube rarely blocks it the way it blocks Railway.
Needs: Docker Desktop (Windows/Mac) or Docker Engine (Linux), and the laptop kept on, awake and online.

## 1. Get the code
```
git clone https://github.com/dev-shubh-madara/yt-paid-api-bot
cd yt-paid-api-bot
```

## 2. Create `.env` (never commit it)
Copy `.env.example` to `.env` and fill in at least:
- `ADMIN_TOKEN` and `KEY_CREATION_SECRET`: long random strings (32+ chars)
- `UPI_ID`, `PAYEE_NAME`, `IMAP_USER`, `IMAP_PASSWORD` (Gmail app password)
- `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` (only needed for the web portal login)
- `PUBLIC_BASE_URL` and `CLERK_AUTHORIZED_PARTIES`: your public link from step 4

Leave `YTDLP_COOKIES_*` and `YTDLP_PROXY` empty. A home IP normally needs neither.

## 3. Start the API
```
docker compose up -d --build
docker compose logs -f api
```
Check on the laptop: http://localhost:8000/health (if you want this, add `ports: ["8000:8000"]` under `api` in docker-compose.yml).

## 4. Give it a public link (pick one)
**A. Quick, no domain (good for testing and for the bot only):**
```
docker compose --profile quick up -d
docker compose logs cloudflared-quick     # look for https://something.trycloudflare.com
```
The link changes whenever it restarts, so you must update the bot's API URL each time. The web portal login (Clerk) will not work reliably on it.

**B. Stable link (recommended once it works):** free Cloudflare account + a domain.
Cloudflare Zero Trust > Networks > Tunnels > create tunnel, copy its token into `.env` as `TUNNEL_TOKEN`,
point your hostname to `http://api:8000`, set `REQUIRE_TRUSTED_PROXY=true`, then:
```
docker compose --profile tunnel up -d
```

## 5. Point the bot at it
Set the bot's API URL to your link and use your API key. (Bot repo: change it there only.)

## 6. Keep it running
- Windows: Settings > Power > Sleep = Never. Docker Desktop > Settings > "Start when you sign in".
  All services use `restart: unless-stopped`, so they come back after a reboot.
- Data (keys, orders) lives in the `ytapi-data` Docker volume. Back it up:
  `docker run --rm -v yt-paid-api-bot_ytapi-data:/d -v "$PWD":/b alpine tar czf /b/backup.tgz -C /d .`
- Update: `git pull && docker compose up -d --build`

## If YouTube still blocks you
Upload cookies from a throwaway Google account (not your main one):
`POST /admin/youtube/cookies` with your `X-Admin-Token` (see README). Status: `GET /admin/youtube`.
