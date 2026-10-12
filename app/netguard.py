"""Client-IP detection, Cloudflare / origin lock, per-IP throttling, security headers.

Why this exists: if you blindly trust X-Forwarded-For (or uvicorn's --proxy-headers
with a wildcard), anyone who can reach the server directly can fake their IP and
walk around every per-IP limit. We only believe forwarded headers when the TCP
peer is a proxy we trust (Cloudflare's published ranges, a Cloudflare Tunnel on
localhost, or CIDRs you list yourself).
"""

import ipaddress
from functools import lru_cache

from fastapi import Request
from fastapi.responses import JSONResponse

# https://www.cloudflare.com/ips-v4 and /ips-v6 (fetched 2026-10-12).
# Cloudflare rarely changes these; update them from those URLs if they do.
CLOUDFLARE_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

PORTAL_PATHS = ("/portal",)


def parse_nets(csv: str) -> list:
    nets = []
    for part in (csv or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _valid_ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


@lru_cache(maxsize=8)
def _cf_nets():
    return [ipaddress.ip_network(r) for r in CLOUDFLARE_RANGES]


def trusted_nets(settings) -> list:
    nets = parse_nets(settings.trusted_proxies)
    if settings.trust_cloudflare:
        nets = nets + _cf_nets()
    return nets


def _in(ip, nets) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def real_ip(request: Request) -> str:
    """The caller's IP. Forwarded headers are used only if the peer is trusted."""
    peer = request.client.host if request.client else ""
    peer_ip = _valid_ip(peer)
    if peer_ip is None:
        return peer or "unknown"
    nets = request.app.state.trusted_nets
    if not nets or not _in(peer_ip, nets):
        # PaaS hosts (Railway, Render...) put their own proxy in front of us and we
        # cannot list its IPs. PROXY_HOPS=N means "N proxies we control sit in front":
        # the real client is the Nth entry from the RIGHT of X-Forwarded-For. Entries
        # further left are client-supplied and ignored, so spoofing does not work.
        hops = request.app.state.settings.proxy_hops
        if hops > 0:
            parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
            if len(parts) >= hops:
                ip = _valid_ip(parts[-hops])
                if ip is not None:
                    return str(ip)
        return str(peer_ip)
    cf = _valid_ip(request.headers.get("cf-connecting-ip"))
    if cf is not None:
        return str(cf)
    # Walk X-Forwarded-For from the right, skipping our own trusted proxies.
    xff = request.headers.get("x-forwarded-for", "")
    for part in reversed([p for p in xff.split(",") if p.strip()]):
        ip = _valid_ip(part)
        if ip is None:
            break
        if not _in(ip, nets):
            return str(ip)
    return str(peer_ip)


def _peer_trusted(request: Request) -> bool:
    peer_ip = _valid_ip(request.client.host if request.client else "")
    return peer_ip is not None and _in(peer_ip, request.app.state.trusted_nets)


def _deny(status: int, error: str, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status, headers=headers)


API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def portal_csp(clerk_domain: str) -> str:
    c = f"https://{clerk_domain}" if clerk_domain else ""
    return (
        f"default-src 'none'; script-src 'self' {c} https://challenges.cloudflare.com; "
        f"connect-src 'self' {c} https://clerk-telemetry.com; "
        f"img-src 'self' https://img.clerk.com data:; style-src 'self' 'unsafe-inline'; "
        f"frame-src https://challenges.cloudflare.com https://accounts.google.com; "
        f"worker-src blob:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )


def install(app, settings) -> None:
    """Attach the guard middleware (runs on every request)."""

    @app.middleware("http")
    async def guard(request: Request, call_next):
        st = request.app.state
        path = request.url.path
        ip = real_ip(request)
        request.state.ip = ip

        # 1) Origin lock: only accept traffic that came through Cloudflare / the tunnel.
        if settings.require_trusted_proxy and not _peer_trusted(request):
            return _deny(403, "forbidden", "Direct access is not allowed.")

        # 2) Host allow-list (stops Host-header tricks / hitting the raw origin by IP).
        if settings.allowed_hosts:
            host = request.headers.get("host", "").split(":")[0].lower()
            if host not in settings.allowed_host_list:
                return _deny(400, "bad_host", "Unknown host.")

        # 3) Admin only from allow-listed IPs, if configured.
        if path.startswith("/admin") and settings.admin_ip_allowlist:
            nets = parse_nets(settings.admin_ip_allowlist)
            ipo = _valid_ip(ip)
            if ipo is None or not _in(ipo, nets):
                return _deny(403, "forbidden", "Admin is not available from this address.")

        # 4) Request size: these endpoints only take tiny JSON bodies.
        if request.method in ("POST", "PUT", "PATCH"):
            limit = 2_000_000 if path == "/admin/parse-email" else 16_384
            cl = request.headers.get("content-length")
            if cl is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
                return _deny(411, "length_required", "Send a Content-Length header.")
            if cl is not None:
                try:
                    too_big = int(cl) > limit
                except ValueError:
                    too_big = True
                if too_big:
                    return _deny(413, "too_large", "Request body too large.")

        # 5) Unauthenticated callers get a tight per-IP limit (login page, key
        #    creation attempts, probing). Callers with a key have per-key limits;
        #    stream links are signed tokens; /admin has its own brute-force guard.
        has_cred = bool(request.headers.get("x-api-key") or request.headers.get("authorization"))
        if not has_cred and not path.startswith(("/admin", "/v1/stream/")) and path != "/health":
            ok, retry = st.limiter.allow(f"anon:{ip}", settings.anon_rate_limit_per_minute, 60)
            if not ok:
                return _deny(429, "rate_limited", "Too many requests from this address.",
                             {"Retry-After": str(retry)})

        response = await call_next(request)

        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith(PORTAL_PATHS):
            if "content-security-policy" not in h:
                h["Content-Security-Policy"] = portal_csp(st.clerk_domain)
        elif path.startswith(("/docs", "/redoc")):
            pass  # swagger UI loads assets from a CDN; only reachable if EXPOSE_DOCS=true
        else:
            h["Content-Security-Policy"] = API_CSP
        if not path.startswith("/v1/stream/"):
            h["Cache-Control"] = "no-store"
        if "server" in h:
            del h["server"]
        return response
