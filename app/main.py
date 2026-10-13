import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import Database
from .clerk_auth import ClerkAuth
from .errors import ApiError
from . import netguard
from .keys import KeyService
from .payments.imap_poller import ImapPoller
from .payments.service import PaymentService
from .ratelimit import SlidingWindowLimiter
from .routes import admin, media, public
from .utils import Clock
from .ytdlp_service import Extractor

log = logging.getLogger("ytapi")


def create_app(settings: Settings | None = None, imap_factory=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        db = Database(os.path.join(settings.data_dir, "api.db"))
        await db.connect()
        clock = Clock(settings.timezone)

        secret = settings.token_secret
        if not secret:
            row = await db.fetchone("SELECT v FROM settings WHERE k='token_secret'")
            if row:
                secret = row["v"]
            else:
                secret = secrets.token_urlsafe(48)
                await db.execute("INSERT INTO settings(k, v) VALUES('token_secret', ?)", (secret,))

        keys = KeyService(db, clock, settings)
        app.state.settings = settings
        app.state.db = db
        app.state.clock = clock
        app.state.token_secret = secret
        app.state.keys = keys
        app.state.payments = PaymentService(db, keys, clock, settings)
        app.state.extractor = Extractor(settings)
        app.state.limiter = SlidingWindowLimiter()
        app.state.trusted_nets = netguard.trusted_nets(settings)
        app.state.clerk_domain = settings.clerk_domain
        app.state.recent_plays = {}
        app.state.poller = None
        app.state.http = httpx.AsyncClient(
            follow_redirects=True,
            trust_env=False,  # only use the proxy we are explicitly given
            proxy=settings.ytdlp_proxy or None,  # googlevideo URLs are bound to the resolving IP
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

        app.state.clerk = ClerkAuth(settings, app.state.http)
        if settings.clerk_publishable_key and not app.state.clerk.enabled:
            log.error("Clerk is half-configured: need a valid CLERK_PUBLISHABLE_KEY and CLERK_SECRET_KEY")
        if settings.require_trusted_proxy and not app.state.trusted_nets:
            log.error("REQUIRE_TRUSTED_PROXY is on but no TRUSTED_PROXIES / TRUST_CLOUDFLARE is set: all requests will be refused")
        if settings.admin_token and len(settings.admin_token) < 24:
            log.warning("ADMIN_TOKEN is short; use at least 32 random characters")
        if not settings.admin_token:
            log.warning("ADMIN_TOKEN is not set: /admin endpoints are disabled")
        task = None
        if settings.imap_enabled:
            if not (settings.imap_user and settings.imap_password):
                log.error("IMAP_ENABLED is true but IMAP_USER / IMAP_PASSWORD are missing")
            else:
                kwargs = {"imap_factory": imap_factory} if imap_factory else {}
                app.state.poller = ImapPoller(settings, db, app.state.payments, clock, **kwargs)
                app.state.payments.poller = app.state.poller
                task = asyncio.create_task(app.state.poller.run_forever())
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await app.state.http.aclose()
            app.state.extractor.shutdown()
            await db.close()

    app = FastAPI(
        title="Madara YouTube API",
        version="1.0.0",
        description="yt-dlp powered song search + streaming with API keys and daily plans. "
        "No YouTube API key is needed.",
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url="/redoc" if settings.expose_docs else None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
    )
    netguard.install(app, settings)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    app.include_router(public.router)
    app.include_router(media.router)
    app.include_router(admin.router)
    return app


app = create_app() if os.environ.get("YTAPI_NO_AUTOAPP") != "1" else None  # type: ignore[assignment]
