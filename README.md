# Madara YT API

A self-hosted music-streaming API for Telegram bots. yt-dlp is the backend, so **no YouTube API key is needed**. It hands out API keys, enforces daily song limits, sells paid plans and verifies FamPay payments over IMAP.

## Plans (daily songs, 30 days per purchase)

| Plan | Songs/day | Price |
|---|---|---|
| free | 1000 | ₹0 |
| p2000 | 2000 | ₹29 |
| p3000 | 3000 | ₹39 |
| p4000 | 4000 | ₹44 |
| p10000 | 10000 | ₹79 |

Limits reset at midnight Asia/Kolkata. A song is counted once per successful `/v1/play` (failures are free, the same song within 60s counts once). Streaming and seeking are never counted.

## Run

```bash
cp .env.example .env     # edit it
docker compose up -d --build
# or: pip install -r requirements.txt && uvicorn app.main:app --port 8000
```

Needs Python 3.12+ and **node or deno** (the Dockerfile includes node). Run a **single worker** and keep `/data` on a persistent volume (SQLite). Put it behind HTTPS (Caddy, nginx, or your host's proxy). Docs live at `/docs`.

## Endpoints

Public: `GET /health`, `GET /v1/plans`, `POST /v1/keys {"owner": "..."}`.
With `X-API-Key` (or `Authorization: Bearer`): `GET /v1/me`, `GET /v1/search?q=`, `GET /v1/track?q=`, `GET /v1/playlist?url=`, `GET /v1/play?q=|id=|url=&type=audio|video` (returns a signed `stream_url`), `GET /v1/stream/{token}`, `POST /v1/orders {"plan": "p3000"}`, `GET /v1/orders/{id}`, `POST /v1/orders/{id}/claim {"utr": "123456789012"}`.
Admin (`X-Admin-Token`): `/admin/stats`, `/admin/keys...` (plan, revoke, activate, rotate, reset-usage), `/admin/orders`, `/admin/orders/{id}/approve`, `/admin/payments`, `/admin/emails`, `/admin/parse-email`.

## Payment flow

1. User calls `POST /v1/orders` and gets a code like `MYTXZDRFW`.
2. User pays the exact amount to your FamPay UPI ID with the code in the note.
3. The IMAP poller reads FamPay's email (read-only), checks the sender and DKIM/DMARC, parses amount/UTR/code, and activates the plan. Paying without the note works too: the user sends the 12-digit UTR to `/claim`.
4. Underpaid is rejected, overpaid is accepted, and you can always approve manually with the admin endpoint.

**Gmail:** turn on 2-step verification, create an App Password, and set `IMAP_USER`/`IMAP_PASSWORD`. Use a dedicated mailbox and forward FamPay's emails to it.
**Important:** FamPay's real email wording and sender are not verified here. Use `/admin/emails` and `/admin/parse-email` to check a real email, then adjust `PAYMENT_SENDERS` and the keyword settings. If emails come through a forwarder and fail the auth check, either whitelist directly or set `REQUIRE_EMAIL_AUTH=false` (less safe).

## Google login (one key per person)

With `CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY` set, `POST /v1/keys` needs a Clerk session token, and users get their key at **`/portal`** ("Continue with Google"). The server verifies the token's signature, then asks Clerk who the user is and accepts only a *verified Google account on @gmail.com*. Addresses are canonicalised (`j.o.h.n+x@gmail.com` = `john@gmail.com`), so one person gets exactly one key, ever. Lost it? `/portal` → "rotate" gives a new key and kills the old one. A key the admin revoked cannot be re-created.
In the Clerk dashboard: enable the Google social connection only (turn off email/password and other methods) and add your domain. Your own bot can still issue keys server-side with `X-Creation-Secret` (`KEY_CREATION_SECRET`).

## Cloudflare and network hardening

Best setup: Cloudflare Tunnel (`docker compose --profile tunnel up -d`, `REQUIRE_TRUSTED_PROXY=true`). The origin has no open port or public IP, so nobody can bypass Cloudflare. Or put the VPS behind Cloudflare's proxy and set `TRUST_CLOUDFLARE=true` + `REQUIRE_TRUSTED_PROXY=true` (also firewall the origin to Cloudflare's ranges).
The real client IP is read from `CF-Connecting-IP` **only** when the TCP peer is trusted, so spoofed headers don't dodge per-IP limits. Also built in: per-IP limits for callers without a key, per-key limits, host allow-list, optional admin IP allow-list, request-size caps, strict security headers/CSP, docs disabled by default, brute-force lockout on the admin token. In Cloudflare also turn on: SSL "Full (strict)", Bot Fight Mode, and a rate-limiting rule on `/v1/keys`.
On Railway/Render/Koyeb set `PROXY_HOPS=1` (and do NOT set `REQUIRE_TRUSTED_PROXY`, `TRUSTED_PROXIES` or `ALLOWED_HOSTS` unless you know the exact values). Without `PROXY_HOPS`, every visitor looks like the platform's proxy and shares one set of per-IP limits.

## Security notes

- Only SHA-256 hashes of API keys are stored, and one active key is allowed per owner.
- No system is "unhackable". This raises the cost of abuse a lot; keep dependencies updated and your secrets private.
- Without Google login, set `KEY_CREATION_SECRET`, otherwise anyone can claim free keys.
- Only YouTube hosts are accepted. Stream links are HMAC-signed and expire after 6h.
- Never commit `.env` or cookies.

## Limitations

Live streams are unsupported. If YouTube asks to "sign in", set `YTDLP_COOKIES_FILE` (use a throwaway account). Datacenter IPs may be blocked by YouTube, in which case use `YTDLP_PROXY`. Keep yt-dlp updated (`pip install -U yt-dlp`).

## Bot integration

Copy `bot_client/Youtube.py` to `MADARAMUSIC/platforms/Youtube.py` in the bot and set `API_URL` and `API_KEY` (optional `STREAM_MODE=url|file`, `DOWNLOAD_DIR`).

## Tests

`pip install -r requirements-dev.txt && pytest`
