from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import gphoto2 as gp

NIKON_USB_VENDOR_ID = "04b0:"

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"
CAPTURE_DIR.mkdir(exist_ok=True)

EVENT_DRAIN_TIMEOUT_MS = 500

# One PTP session, held open for the backend's whole lifetime, instead of a
# fresh gphoto2 CLI process (and fresh session) per request - that one-shot
# approach cost ~0.5s of connect/disconnect overhead per frame and made the
# camera visibly flash "connecting to computer" every second. A single
# Camera object can't be touched from two threads at once, so all access
# still goes through one lock.
_lock = threading.Lock()
_camera: gp.Camera | None = None


def _get_camera() -> gp.Camera:
    global _camera
    if _camera is None:
        _camera = gp.Camera()
        _camera.init()
    return _camera


def _reset_camera() -> None:
    global _camera
    if _camera is not None:
        try:
            _camera.exit()
        except gp.GPhoto2Error:
            pass
    _camera = None


def _require_camera() -> gp.Camera:
    if _camera is None:
        raise RuntimeError("Camera not connected - call connect() first")
    return _camera


def _model_name(camera: gp.Camera) -> str:
    summary = str(camera.get_summary())
    model_line = next((line for line in summary.splitlines() if line.startswith("Model:")), None)
    return model_line.split(":", 1)[1].strip() if model_line else "Unknown"


def is_connected() -> bool:
    return _camera is not None


def connect() -> dict:
    with _lock:
        try:
            camera = _get_camera()
            return {"connected": True, "model": _model_name(camera)}
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e


def disconnect() -> dict:
    with _lock:
        _reset_camera()
        return {"connected": False}


def detect_camera() -> dict:
    """Lightweight USB-bus presence check - does NOT open a PTP session, so
    it's safe to call regardless of whether connect() has been called, and
    doesn't itself hold the camera or drain its battery.
    """
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"connected": False, "model": None}

    for line in result.stdout.splitlines():
        if NIKON_USB_VENDOR_ID in line:
            # e.g. "...ID 04b0:0453 Nikon Corp. NIKON DSC Z f" -> "Nikon Corp. NIKON DSC Z f"
            remainder = line.split(NIKON_USB_VENDOR_ID, 1)[1].strip()
            model = remainder.split(" ", 1)[1] if " " in remainder else remainder
            return {"connected": True, "model": model}
    return {"connected": False, "model": None}


SETTINGS = [
    ("iso", "ISO"),
    ("shutterspeed", "Shutter Speed"),
    ("f-number", "Aperture"),
    ("exposurecompensation", "Exposure Comp"),
    ("whitebalance", "White Balance"),
    ("focusmode", "Focus Mode"),
    ("imagequality", "Image Quality"),
    ("imagesize", "Image Size"),
    ("flashmode", "Flash Mode"),
]


def read_settings() -> list[dict]:
    with _lock:
        try:
            camera = _require_camera()
            config = camera.get_config()
            results = []
            for name, label in SETTINGS:
                try:
                    child = config.get_child_by_name(name)
                    results.append(
                        {
                            "key": name,
                            "label": label,
                            "value": str(child.get_value()),
                            "readonly": bool(child.get_readonly()),
                        }
                    )
                except gp.GPhoto2Error:
                    continue  # property not present on this camera/firmware
            return results
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e


def capture_preview() -> bytes:
    with _lock:
        try:
            camera = _require_camera()
            preview = camera.capture_preview()
            return bytes(preview.get_data_and_size())
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e


def capture_photo() -> bytes:
    """Capture a photo and return the JPEG bytes for display. If the camera is
    set to RAW+JPEG, the RAW file is still downloaded to CAPTURE_DIR (for
    later retrieval) but only the JPEG is returned here.
    """
    with _lock:
        try:
            camera = _require_camera()
            first = camera.capture(gp.GP_CAPTURE_IMAGE)
            files = [first]

            # Nikon (and others) fire a separate FILE_ADDED event for each
            # additional file from one shutter release (e.g. the RAW half
            # of a RAW+JPEG capture) - drain those before moving on.
            while True:
                event_type, event_data = camera.wait_for_event(EVENT_DRAIN_TIMEOUT_MS)
                if event_type == gp.GP_EVENT_FILE_ADDED:
                    files.append(event_data)
                elif event_type == gp.GP_EVENT_TIMEOUT:
                    break

            stamp = time.strftime("%Y%m%d-%H%M%S")
            jpeg_bytes: bytes | None = None
            for i, f in enumerate(files):
                camera_file = camera.file_get(f.folder, f.name, gp.GP_FILE_TYPE_NORMAL)
                data = bytes(camera_file.get_data_and_size())
                ext = Path(f.name).suffix
                (CAPTURE_DIR / f"{stamp}-{i}{ext}").write_bytes(data)
                if ext.lower() in (".jpg", ".jpeg") and jpeg_bytes is None:
                    jpeg_bytes = data
                camera.file_delete(f.folder, f.name)

            if jpeg_bytes is None:
                raise RuntimeError(f"Capture succeeded but no JPEG found among: {[f.name for f in files]}")
            return jpeg_bytes
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e


def list_card_files() -> list[dict]:
    """List every file on the camera's storage, newest first (by mtime)."""
    with _lock:
        try:
            camera = _require_camera()
            entries = []

            def walk(path: str) -> None:
                for name, _ in camera.folder_list_folders(path):
                    walk(path.rstrip("/") + "/" + name)
                for name, _ in camera.folder_list_files(path):
                    info = camera.file_get_info(path, name)
                    entries.append({"folder": path, "name": name, "mtime": info.file.mtime})

            walk("/")
            entries.sort(key=lambda e: e["mtime"], reverse=True)
            return entries
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e


def download_card_file(folder: str, name: str) -> bytes:
    with _lock:
        try:
            camera = _require_camera()
            camera_file = camera.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
            return bytes(camera_file.get_data_and_size())
        except gp.GPhoto2Error as e:
            _reset_camera()
            raise RuntimeError(str(e)) from e
