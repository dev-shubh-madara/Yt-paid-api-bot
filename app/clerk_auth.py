"""Sign-in with Google through Clerk: one API key per real Google account.

Flow: the browser signs in with Clerk, gets a short-lived session JWT, sends it
to us. We (1) verify the JWT signature against Clerk's public keys, (2) ask
Clerk's backend API (with the secret key) who that user really is, and (3) only
accept a verified Google account whose Gmail address is canonicalised, so
j.o.h.n+1@gmail.com and john@gmail.com count as the same person.
"""

import base64
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

from .errors import ApiError

log = logging.getLogger("ytapi.clerk")


def domain_from_publishable_key(pk: str) -> str:
    """pk_test_<base64('xxx.clerk.accounts.dev$')> -> 'xxx.clerk.accounts.dev'."""
    try:
        b64 = pk.split("_", 2)[2]
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode().rstrip("$").strip()
    except Exception:
        return ""


def canonical_gmail(email: str) -> str | None:
    """Return the canonical gmail.com address, or None if it is not a Gmail address."""
    email = (email or "").strip().lower()
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if domain not in ("gmail.com", "googlemail.com"):
        return None
    local = local.split("+", 1)[0].replace(".", "")
    if not local:
        return None
    return f"{local}@gmail.com"


@dataclass
class GoogleIdentity:
    clerk_user_id: str
    email: str  # canonical
    raw_email: str


class ClerkAuth:
    def __init__(self, settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self.domain = settings.clerk_domain
        self.issuer = f"https://{self.domain}" if self.domain else ""
        self._jwks: dict[str, object] = {}
        self._jwks_at = 0.0
        self._jwks_try = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.domain and self.s.clerk_secret_key)

    async def _refresh_jwks(self, force: bool) -> None:
        now = time.monotonic()
        if not force and self._jwks and now - self._jwks_at < 3600:
            return
        if now - self._jwks_try < 30:  # never hammer Clerk
            return
        self._jwks_try = now
        r = await self.http.get(f"{self.issuer}/.well-known/jwks.json", timeout=10)
        r.raise_for_status()
        keys = {}
        for jwk in r.json().get("keys", []):
            if jwk.get("kty") == "RSA" and jwk.get("kid"):
                keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        if keys:
            self._jwks, self._jwks_at = keys, now

    async def verify_session(self, token: str) -> str:
        """Check signature, issuer, expiry and origin. Returns the Clerk user id."""
        bad = ApiError(401, "invalid_login", "Your login session is invalid or expired. Sign in again.")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                log.warning("login rejected: token alg is %r, not RS256", header.get("alg"))
                raise bad
            kid = header.get("kid")
            try:
                await self._refresh_jwks(force=False)
                if kid not in self._jwks:
                    await self._refresh_jwks(force=True)
            except httpx.HTTPError:
                raise ApiError(503, "login_unavailable", "Login provider is unreachable. Try again shortly.")
            key = self._jwks.get(kid)
            if key is None:
                log.warning("login rejected: signing key %r not found in Clerk JWKS for %s", kid, self.issuer)
                raise bad
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
                leeway=5,
            )
        except ApiError:
            raise
        except jwt.PyJWTError as e:
            # Never log the token itself, only why it was refused.
            log.warning("login rejected: %s: %s (expected issuer %s)", type(e).__name__, e, self.issuer)
            raise bad
        # 'azp' is the page origin that asked for the token; reject tokens minted elsewhere.
        allowed = self.s.clerk_authorized_party_list
        azp = claims.get("azp")
        if allowed and azp and azp not in allowed:
            log.warning("login rejected: token origin (azp) %r is not in CLERK_AUTHORIZED_PARTIES %r", azp, allowed)
            raise bad
        if claims.get("sts") == "pending":  # sign-in not finished (e.g. missing MFA step)
            log.warning("login rejected: session is still pending")
            raise bad
        return str(claims["sub"])

    async def google_identity(self, user_id: str) -> GoogleIdentity:
        """Ask Clerk who this user is and require a verified Google/Gmail account."""
        try:
            r = await self.http.get(
                f"https://api.clerk.com/v1/users/{user_id}",
                headers={"Authorization": f"Bearer {self.s.clerk_secret_key}"},
                timeout=10,
            )
        except httpx.HTTPError:
            raise ApiError(503, "login_unavailable", "Login provider is unreachable. Try again shortly.")
        if r.status_code != 200:
            log.warning("Clerk user lookup failed with HTTP %s (check CLERK_SECRET_KEY belongs to the same app)", r.status_code)
            raise ApiError(401, "invalid_login", "Could not confirm your account. Sign in again.")
        u = r.json()
        if u.get("banned") or u.get("locked"):
            raise ApiError(403, "account_blocked", "This account is not allowed to get a key.")

        google_emails = set()
        for ea in u.get("external_accounts") or []:
            prov = str(ea.get("provider") or ea.get("object") or "")
            ver = (ea.get("verification") or {}).get("status")
            if "google" in prov and ver == "verified" and ea.get("email_address"):
                google_emails.add(ea["email_address"].strip().lower())
        primary_id = u.get("primary_email_address_id")
        candidates = []
        for e in u.get("email_addresses") or []:
            ver = (e.get("verification") or {}).get("status")
            addr = (e.get("email_address") or "").strip().lower()
            if ver == "verified" and addr in google_emails and e.get("id") == primary_id:
                candidates.append(addr)
        if not candidates:
            raise ApiError(
                403, "google_required",
                "Sign in with a verified Google account (Gmail). Other sign-in methods are not accepted.",
            )
        raw = candidates[0]
        canon = canonical_gmail(raw)
        if canon is None and self.s.require_gmail:
            raise ApiError(403, "gmail_required", "Only @gmail.com accounts can get a key.")
        return GoogleIdentity(user_id, canon or raw, raw)
