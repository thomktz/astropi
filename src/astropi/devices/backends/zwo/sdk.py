"""Loading ZWO's ASI SDK, and finding the cameras on the bus.

The SDK is a closed-source shared library (`libASICamera2.so`) that ZWO
ships separately from any Python package; `zwoasi` is only the ctypes
binding over it. So there are two things that can be missing, and they
fail differently: no `zwoasi` is an install problem, no library is a
deployment one. Both are reported here, once, in words that say which.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from astropi.core.errors import DeviceError, DeviceNotFoundError

logger = logging.getLogger(__name__)

#: Where the library is looked for when no path is configured, in order.
#: The first is where the Pi's install puts it; the rest are where the
#: ZWO tarball and the Debian `libasi` package do.
DEFAULT_LIBRARY_PATHS = (
    Path.home() / "zwo-sdk" / "libASICamera2.so",
    Path("/usr/local/lib/libASICamera2.so"),
    Path("/usr/lib/aarch64-linux-gnu/libASICamera2.so.1"),
    Path("/usr/lib/libASICamera2.so"),
)

_init_lock = threading.Lock()
_initialised: str | None = None


class SdkCamera(Protocol):
    """The part of `zwoasi.Camera` the backend uses. Tests supply a fake."""

    def get_camera_property(self) -> dict[str, Any]: ...
    def get_controls(self) -> dict[str, dict[str, Any]]: ...
    def get_control_value(self, control_type: int) -> list: ...
    def set_control_value(self, control_type: int, value: int, auto: bool = False) -> None: ...
    def set_roi(
        self,
        start_x: int | None = None,
        start_y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        bins: int | None = None,
        image_type: int | None = None,
    ) -> None: ...
    def start_exposure(self, is_dark: bool = False) -> None: ...
    def start_video_capture(self) -> None: ...
    def stop_video_capture(self) -> None: ...
    def get_video_data(self, timeout: int | None = None, buffer_: bytearray | None = None) -> bytearray: ...
    def stop_exposure(self) -> None: ...
    def get_exposure_status(self) -> int: ...
    def get_data_after_exposure(self, buffer_: bytearray | None = None) -> bytearray: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FoundCamera:
    index: int
    name: str
    width: int
    height: int


class Sdk(Protocol):
    def cameras(self) -> list[FoundCamera]: ...
    def open(self, index: int) -> SdkCamera: ...


class ZwoSdk:
    """The real SDK. Imported and initialised on first use, not at import.

    The library holds process-wide state, and `zwoasi.init` may only be
    called once per process, so both sensors of a Duo share one of these.
    """

    def __init__(self, library_path: str | None = None) -> None:
        self._library_path = library_path

    def _asi(self):
        global _initialised
        try:
            import zwoasi
        except ImportError as error:  # pragma: no cover - depends on the install
            raise DeviceError("the zwoasi package is not installed; run `uv sync`") from error

        with _init_lock:
            if _initialised is None:
                path = self._resolve_library()
                zwoasi.init(path)
                _initialised = path
                logger.info("loaded the ZWO ASI SDK from %s", path)
        return zwoasi

    def _resolve_library(self) -> str:
        if self._library_path:
            if not Path(self._library_path).exists():
                raise DeviceError(f"no ZWO SDK library at {self._library_path}")
            return self._library_path
        for candidate in DEFAULT_LIBRARY_PATHS:
            if candidate.exists():
                return str(candidate)
        raise DeviceError(
            "ZWO's libASICamera2.so was not found. Set ASTROPI_ZWO_SDK_PATH to it, "
            f"or put it at {DEFAULT_LIBRARY_PATHS[0]}."
        )

    def cameras(self) -> list[FoundCamera]:
        asi = self._asi()
        found = []
        for index in range(asi.get_num_cameras()):
            try:
                props = asi._get_camera_property(index)
            except asi.ZWO_Error as error:
                # "Camera removed" here almost always means the USB device
                # node is not ours to open - the udev rule is missing.
                raise DeviceError(
                    f"ZWO camera {index} is on the bus but could not be read ({error}). "
                    "Check that the udev rule for vendor 03c3 is installed."
                ) from error
            found.append(
                FoundCamera(
                    index=index,
                    name=str(props["Name"]),
                    width=int(props["MaxWidth"]),
                    height=int(props["MaxHeight"]),
                )
            )
        return found

    def open(self, index: int) -> SdkCamera:
        return self._asi().Camera(index)


def choose(
    found: list[FoundCamera], match: str | None, *, largest: bool, exclude: int | None = None
) -> FoundCamera:
    """Pick one sensor from those on the bus.

    A Duo shows up as two cameras with nothing in their names that says
    which is the guide sensor in every SDK version, so the default is by
    size: the imaging sensor is the big one. `match` overrides that with
    a case-insensitive piece of the name, for a rig with a separate guide
    camera or two cameras of similar size.
    """
    candidates = [camera for camera in found if camera.index != exclude]
    if match:
        candidates = [camera for camera in candidates if match.lower() in camera.name.lower()]
    if not candidates:
        names = ", ".join(camera.name for camera in found) or "none"
        wanted = f"matching {match!r}" if match else "to use"
        raise DeviceNotFoundError(f"no ZWO camera {wanted}; on the bus: {names}")
    candidates.sort(key=lambda camera: camera.width * camera.height, reverse=largest)
    return candidates[0]
