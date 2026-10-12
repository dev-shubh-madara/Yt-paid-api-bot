import base64
import hashlib
import hmac
import json
import secrets

KEY_PREFIX = "myk_"


def new_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# Tokens end up inside URLs that the Telegram bot stores in its queue, and the
# bot decides how to play an item with checks like `"vid_" in file`. An
# underscore-free alphabet makes it impossible for a token to trip those checks.
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode().replace("_", "~")


def _unb64(text: str) -> bytes:
    text = text.replace("~", "_")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(secret: str, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(secret: str, token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64(body))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def safe_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
