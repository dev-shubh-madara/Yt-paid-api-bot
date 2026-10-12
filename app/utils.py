import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class Clock:
    """The one place that knows what 'now' is, so tests can move time."""

    def __init__(self, tz_name: str):
        self.tz = ZoneInfo(tz_name)
        self._offset = 0.0

    def now_ts(self) -> int:
        return int(time.time() + self._offset)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.now_ts(), self.tz)

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def next_reset_ts(self) -> int:
        now = self.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(tomorrow.timestamp())

    def iso(self, ts: int | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds")

    def advance(self, seconds: float) -> None:  # tests only
        self._offset += seconds


def fmt_duration(seconds: int | float | None) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
