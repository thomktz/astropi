"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from astropi.api.routes import api_router
from astropi.api.ws import router as ws_router
from astropi.config import Settings, load_settings
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

    return app


app = create_app()
