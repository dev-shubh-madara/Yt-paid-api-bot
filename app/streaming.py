"""Audio/video proxy.

googlevideo URLs are locked to the IP that resolved them, so the bot cannot
fetch them directly from another machine. This server fetches the media and
passes it through, including HTTP Range requests (needed for seeking).
"""

import logging

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .errors import MediaError
from .ytdlp_service import Extractor, StreamInfo

log = logging.getLogger("ytapi.stream")

PASS_THROUGH = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
)


async def _open(client: httpx.AsyncClient, method: str, info: StreamInfo, range_header: str | None):
    headers = dict(info.headers)
    headers["Accept-Encoding"] = "identity"
    if range_header:
        headers["Range"] = range_header
    request = client.build_request("GET", info.url, headers=headers)  # HEAD is answered from a GET
    return await client.send(request, stream=True)


async def proxy_stream(
    request: Request,
    client: httpx.AsyncClient,
    extractor: Extractor,
    info: StreamInfo,
) -> Response:
    range_header = request.headers.get("range")
    try:
        upstream = await _open(client, request.method, info, range_header)
        if upstream.status_code in (403, 404, 410):
            # Cached URL went stale: resolve a fresh one and try once more.
            await upstream.aclose()
            extractor.invalidate(info.video_id, info.kind)
            info = await extractor.stream_info(info.video_id, info.kind)
            upstream = await _open(client, request.method, info, range_header)
    except httpx.HTTPError as exc:
        log.warning("upstream error for %s: %s", info.video_id, exc)
        raise MediaError(502, "upstream_error", "Could not reach the media server.")

    if upstream.status_code >= 400 and upstream.status_code != 416:
        status = upstream.status_code
        await upstream.aclose()
        raise MediaError(502, "upstream_status", f"Media server answered {status}.")

    out_headers = {h: upstream.headers[h] for h in PASS_THROUGH if h in upstream.headers}
    out_headers.setdefault("accept-ranges", "bytes")
    out_headers["cache-control"] = "private, no-store"

    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=out_headers)

    async def body():
        try:
            async for chunk in upstream.aiter_raw(65536):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=out_headers)
