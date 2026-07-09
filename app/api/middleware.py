"""
Cross-cutting HTTP middleware, wired in one place (create_app calls setup()).

  • CORS           — configurable via API_CORS_ORIGINS so the frontend origin
                     is an .env change, not a code change.
  • request timing — X-Process-Time response header + one access-log line per
                     request; chat calls hit external APIs (embeddings + LLM),
                     so per-request latency visibility matters.
  • GZip           — chat responses carry Arabic context chunks (very
                     compressible text); compression cuts payload size a lot
                     for slow connections.
"""

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import config


async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time"] = f"{elapsed:.3f}"
    print(f"🌐 {request.method} {request.url.path} → {response.status_code} ({elapsed:.2f}s)")
    return response


def setup(app: FastAPI) -> None:
    app.middleware("http")(timing_middleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_credentials=False,  # flip on only with explicit origins (not "*")
        allow_methods=["*"],
        allow_headers=["*"],
    )
