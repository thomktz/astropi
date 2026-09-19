from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from . import gphoto
from .align import goto_and_align
from .camera import MockCamera
from .mount import MockMount, RaDec
from .platesolve import MockPlateSolver

app = FastAPI(title="astropi")

# LAN-only hobby setup, no auth/cookies involved - open CORS is fine here
# since the frontend may be reached from any device's browser on the network.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mount = MockMount()
camera = MockCamera(mount)
solver = MockPlateSolver()

MountState = Literal["idle", "slewing", "tracking", "parked"]
state: MountState = "idle"
target: RaDec | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _status_response() -> dict:
    pos = mount.position()
    return {
        "state": state,
        "ra_deg": pos.ra_deg,
        "dec_deg": pos.dec_deg,
        "target": {"ra_deg": target.ra_deg, "dec_deg": target.dec_deg} if target else None,
    }


@app.get("/status")
def status() -> dict:
    return _status_response()


class GotoRequest(BaseModel):
    ra_deg: float
    dec_deg: float


@app.post("/goto")
def goto(req: GotoRequest) -> dict:
    global state, target
    target = RaDec(req.ra_deg, req.dec_deg)
    state = "slewing"

    result = goto_and_align(target, mount, camera, solver)
    state = "tracking" if result.converged else "idle"

    return {
        **_status_response(),
        "converged": result.converged,
        "iterations": len(result.steps),
        "steps": [{"ra_deg": s.solved.ra_deg, "dec_deg": s.solved.dec_deg, "error_deg": s.error_deg} for s in result.steps],
    }


@app.post("/park")
def park() -> dict:
    global state, target
    mount.park()
    target = None
    state = "parked"
    return _status_response()


@app.get("/camera/detect")
def camera_detect() -> dict:
    return gphoto.detect_camera()


@app.get("/camera/connection")
def camera_connection() -> dict:
    return {"connected": gphoto.is_connected()}


@app.post("/camera/connect")
def camera_connect() -> dict:
    try:
        return gphoto.connect()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/camera/disconnect")
def camera_disconnect() -> dict:
    return gphoto.disconnect()


@app.get("/camera/preview")
def camera_preview() -> Response:
    try:
        return Response(content=gphoto.capture_preview(), media_type="image/jpeg")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/camera/capture")
def camera_capture() -> Response:
    try:
        return Response(content=gphoto.capture_photo(), media_type="image/jpeg")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/camera/settings")
def camera_settings() -> list[dict]:
    try:
        return gphoto.read_settings()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/camera/files")
def camera_files() -> list[dict]:
    try:
        return gphoto.list_card_files()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/camera/files/download")
def camera_file_download(folder: str, name: str) -> Response:
    try:
        data = gphoto.download_card_file(folder, name)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    media_type = "image/x-nikon-nef" if name.lower().endswith(".nef") else "image/jpeg"
    return Response(content=data, media_type=media_type)
