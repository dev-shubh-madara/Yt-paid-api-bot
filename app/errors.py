class ApiError(Exception):
    """Any error that should be returned to the caller as JSON."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        **extra,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class MediaError(ApiError):
    """YouTube / yt-dlp related failure."""
