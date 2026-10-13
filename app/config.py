"""All settings come from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Server ────────────────────────────────────────────────────────────
    # Public URL of this API, used when building stream links, e.g.
    # https://yt-api.example.com  (if empty it is derived from the request).
    public_base_url: str = ""
    data_dir: str = "data"  # SQLite lives here. MOUNT A VOLUME on cloud hosts.
    timezone: str = "Asia/Kolkata"  # "daily" limits reset at midnight in this zone

    # ── Secrets ───────────────────────────────────────────────────────────
    admin_token: str = ""  # empty = admin endpoints disabled
    token_secret: str = ""  # empty = generated once and stored in the database
    token_ttl_seconds: int = 6 * 3600  # how long a stream link stays valid

    # ── API keys ──────────────────────────────────────────────────────────
    allow_public_key_creation: bool = True
    # If set, POST /v1/keys needs header X-Creation-Secret: <this>. Use it when
    # only your own bot should hand out keys (best protection against abuse).
    key_creation_secret: str = ""
    key_creations_per_ip_per_hour: int = 5
    rate_limit_per_minute: int = 120  # requests per key per minute (not songs)
    play_dedupe_seconds: int = 60  # same key + same song inside this window = 1 play

    # ── Google login (Clerk) ──────────────────────────────────────────────
    # With these set, POST /v1/keys needs a Clerk session token: one key per
    # real, verified Google account. Get both from the Clerk dashboard (API keys).
    clerk_publishable_key: str = ""  # pk_test_... / pk_live_...
    clerk_secret_key: str = ""  # sk_test_... / sk_live_... (keep secret!)
    clerk_authorized_parties: str = ""  # page origins allowed to mint tokens, e.g. https://yt-api.example.com
    require_gmail: bool = True  # only @gmail.com (Workspace domains can mint endless addresses)

    # ── Network hardening ─────────────────────────────────────────────────
    # Forwarded headers (CF-Connecting-IP / X-Forwarded-For) are believed ONLY
    # when the TCP peer is trusted. Cloudflare Tunnel: trusted_proxies=127.0.0.1/32
    # (or your docker network). Cloudflare proxy in front of a VPS: trust_cloudflare=true.
    # Hosting platforms (Railway/Render/Koyeb): number of platform proxies in front of
    # the app. 1 = read the client IP from the last X-Forwarded-For entry. Leave 0 on a VPS.
    proxy_hops: int = 0
    trust_cloudflare: bool = False
    trusted_proxies: str = ""  # comma-separated CIDRs
    # Reject every request that does not come from a trusted proxy (origin lock).
    require_trusted_proxy: bool = False
    allowed_hosts: str = ""  # e.g. yt-api.example.com  (empty = any)
    admin_ip_allowlist: str = ""  # CIDRs allowed to reach /admin (empty = any, token still needed)
    anon_rate_limit_per_minute: int = 60  # per IP, for callers that send no key
    expose_docs: bool = False  # /docs, /redoc, /openapi.json

    # ── Paid plans / payments ─────────────────────────────────────────────
    plan_duration_days: int = 30
    order_ttl_hours: int = 24
    order_grace_hours: int = 6  # payment emails can lag behind the payment
    order_prefix: str = "MYT"  # code the user types in the UPI note
    upi_id: str = ""  # your FamPay UPI ID shown to buyers
    payee_name: str = ""

    # ── IMAP payment verification ─────────────────────────────────────────
    imap_enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""  # Gmail: use an App Password
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 20
    imap_lookback_days: int = 2  # only used on the very first run
    # Only emails whose From address matches one of these are trusted.
    # An entry with "@" is an exact address, otherwise it is a domain.
    payment_senders: str = "fampay.in,famapp.in"
    # Require the receiving mail server to confirm the sender (DMARC/DKIM).
    # Without this anyone could email you a fake "payment received".
    require_email_auth: bool = True
    payment_in_keywords: str = "received,credited,paid you,sent you,money in,payment from,deposit"
    payment_out_keywords: str = (
        "debited,you paid,you sent,paid to,sent to,payment to,refund,failed,declined,reversed"
    )
    utr_pattern: str = r"[0-9]{12}"  # shape of the UPI reference number

    # ── yt-dlp ────────────────────────────────────────────────────────────
    ytdlp_workers: int = 8
    ytdlp_js_runtimes: str = "deno,node"  # YouTube needs a JS runtime for yt-dlp
    ytdlp_cookies_file: str = ""  # optional, only if YouTube asks to "sign in"
    ytdlp_cookies_b64: str = ""  # cookies.txt as base64 (easy on Railway, no file upload needed)
    ytdlp_cookies_text: str = ""  # or the cookies.txt content itself (use \n for new lines)
    ytdlp_proxy: str = ""  # optional; stream proxying uses it too
    mirror_fallback: bool = True  # public Piped/Invidious servers when YouTube blocks this host
    ytdlp_player_clients: str = ""  # optional, e.g. "tv,android"
    url_cache_seconds: int = 3300  # how long a resolved stream URL is reused
    search_cache_seconds: int = 300
    extract_timeout_seconds: int = 45
    max_duration_seconds: int = 0  # 0 = no limit

    @property
    def clerk_domain(self) -> str:
        from .clerk_auth import domain_from_publishable_key

        return domain_from_publishable_key(self.clerk_publishable_key)

    @property
    def clerk_authorized_party_list(self) -> list[str]:
        return [x.strip().rstrip("/") for x in self.clerk_authorized_parties.split(",") if x.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return _csv(self.allowed_hosts)

    @property
    def payment_sender_list(self) -> list[str]:
        return _csv(self.payment_senders)

    @property
    def in_keywords(self) -> list[str]:
        return _csv(self.payment_in_keywords)

    @property
    def out_keywords(self) -> list[str]:
        return _csv(self.payment_out_keywords)

    @property
    def js_runtime_list(self) -> list[str]:
        return _csv(self.ytdlp_js_runtimes)

    @property
    def player_client_list(self) -> list[str]:
        return _csv(self.ytdlp_player_clients)


@lru_cache
def get_settings() -> Settings:
    return Settings()
