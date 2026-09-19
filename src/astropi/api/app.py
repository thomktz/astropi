"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from astropi.api.routes import api_router
from astropi.api.ws import router as ws_router
from astropi.config import PROJECT_ROOT, Settings, load_settings
from astropi.core.errors import (
    AstropiError,
    CapabilityError,
    DeviceBusyError,
    DeviceNotFoundError,
    NotConnectedError,
    SafetyError,
    SolveFailedError,
)
from astropi.runtime import Observatory

logger = logging.getLogger(__name__)

#: Domain errors mapped to the status code that describes them.
ERROR_STATUS: list[tuple[type[Exception], int]] = [
    (DeviceNotFoundError, 404),
    (NotConnectedError, 409),
    (DeviceBusyError, 409),
    (CapabilityError, 501),
    (SafetyError, 422),
    (SolveFailedError, 422),
    (AstropiError, 400),
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.observatory = await Observatory.build(settings)
        logger.info("observatory ready on the %s backend", settings.backend)
        try:
            yield
        finally:
            await app.state.observatory.shutdown()

    app = FastAPI(
        title="astropi",
        version="0.1.0",
        summary="Control software for an astrophotography rig.",
        lifespan=lifespan,
    )

    # The dashboard is opened from phones and laptops all over the LAN, and
    # there is no auth or cookie to protect - this runs on a private network
    # behind a router. Lock this down before exposing the Pi to the internet.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(ws_router)

    @app.exception_handler(AstropiError)
    async def handle_domain_error(request: Request, error: AstropiError) -> JSONResponse:
        """One place that turns domain failures into HTTP responses.

        Without this every handler would need its own try/except to avoid
        reporting "no focuser connected" as a 500 Internal Server Error.
        """
        for error_type, status in ERROR_STATUS:
            if isinstance(error, error_type):
                return JSONResponse(
                    status_code=status,
                    content={"detail": str(error), "error": type(error).__name__},
                )
        return JSONResponse(status_code=400, content={"detail": str(error)})

    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built dashboard, when there is one.

    On the Pi this makes the whole thing one process on one port, so the
    client's same-origin `/api` paths hold in production exactly as they do
    behind Vite's proxy in development. If `web/dist` is absent - a source
    checkout that has not been built - the API still runs on its own and the
    dashboard is served by `npm run dev` instead.
    """
    dist = PROJECT_ROOT / "web" / "dist"
    if not (dist / "index.html").exists():
        logger.info("no built dashboard at %s; serving the API only", dist)
        return

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def dashboard(path: str) -> FileResponse:
        """Hand any unmatched path to the single-page app.

        Registered last, so it cannot shadow the API or the WebSocket. A
        file that exists is served directly; everything else gets
        index.html, which is what client-side routing needs.
        """
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    logger.info("serving the dashboard from %s", dist)


app = create_app()
