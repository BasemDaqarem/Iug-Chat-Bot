"""
Cross-cutting HTTP middleware, wired in one place (create_app calls setup()).

  • CORS           — configurable via API_CORS_ORIGINS so the frontend origin
                     is an .env change, not a code change.
  • request timing — X-Process-Time response header + one access-log line per
                     request; chat calls hit external APIs (embeddings + LLM),
                     so per-request latency visibility matters.
  • GZip           — chat responses carry Arabic context chunks (very
                     compressible text); compression cuts payload size a lot
                     for slow connections. The token-by-token streaming route
                     is EXEMPT: gzip buffers to accumulate bytes before
                     flushing, which collapses a live stream back into one late
                     chunk — the exact bug that made streaming feel non-streamed.
"""

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import config
from app.log import get_logger

log = get_logger("api")


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZip everything EXCEPT streaming endpoints. Compressing a token stream
    holds bytes back until the compressor's buffer fills, destroying the
    incremental delivery, so those paths pass through uncompressed."""

    def __init__(self, app, *, exempt_suffixes=("/stream",), **kwargs):
        super().__init__(app, **kwargs)
        self._exempt = tuple(exempt_suffixes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").endswith(self._exempt):
            await self.app(scope, receive, send)   # bypass gzip → true streaming
            return
        await super().__call__(scope, receive, send)


async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time"] = f"{elapsed:.3f}"
    # ترويسات أمنية أساسية — التطبيق يقدّم واجهات HTML (دخول/أدمن) بنفسه.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    log.info("🌐 %s %s → %d (%.2fs)",
             request.method, request.url.path, response.status_code, elapsed)
    return response


def setup(app: FastAPI) -> None:
    app.middleware("http")(timing_middleware)
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_credentials=False,  # flip on only with explicit origins (not "*")
        allow_methods=["*"],
        allow_headers=["*"],
    )
