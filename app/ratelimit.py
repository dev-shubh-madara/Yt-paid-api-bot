import time
from collections import deque


class SlidingWindowLimiter:
    """Tiny in-memory limiter (per process). Fine for one API instance."""

    def __init__(self):
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def allow(self, key: str, limit: int, window: float) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        self._calls += 1
        if self._calls % 2000 == 0:
            self._gc(now, window)
        if len(dq) >= limit:
            return False, max(1, int(window - (now - dq[0])) + 1)
        dq.append(now)
        return True, 0

    def _gc(self, now: float, window: float) -> None:
        for k in [k for k, dq in self._hits.items() if not dq or now - dq[-1] >= window]:
            del self._hits[k]
