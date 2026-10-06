from fastapi import APIRouter

from astropi.api.routes import (
    camera,
    devices,
    guiding,
    imaging,
    mount,
    polar,
    sessions,
    system,
    targets,
    tasks,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(devices.router)
api_router.include_router(mount.router)
api_router.include_router(camera.router)
api_router.include_router(guiding.router)
api_router.include_router(targets.router)
api_router.include_router(tasks.router)
api_router.include_router(polar.router)
api_router.include_router(sessions.router)
api_router.include_router(imaging.router)
api_router.include_router(imaging.library_router)

__all__ = ["api_router"]
