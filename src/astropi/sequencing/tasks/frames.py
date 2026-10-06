"""One group of a session's frames: its lights, darks, flats or dark flats.

Started by hand, one group at a time, when the rig is ready for it. Each
run brings the sensor to the group's temperature first, flats find their
exposure first, and every frame is filed in the session's folder under its
type - or, for darks shot into the library, in the library's set for those
settings. The group's count of captured frames is kept on the session as
each one lands, so a stopped run shows how far it got.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError, CapabilityError
from astropi.devices.camera import FrameKind
from astropi.sequencing.tasks.calibration import FLICKER_WARN_S, find_flat_exposure
from astropi.sequencing.tasks.capture import CapturePlan, CaptureSequenceTask
from astropi.storage.imaging import group_filter, relink
from astropi.storage.naming import ImageType, session_folder

if TYPE_CHECKING:
    from astropi.runtime import Observatory

logger = logging.getLogger(__name__)

LABELS = {"light": "Lights", "flat": "Flats", "darkflat": "Dark flats", "dark": "Darks"}
TYPES = {
    "light": (ImageType.LIGHT, FrameKind.LIGHT),
    "flat": (ImageType.FLAT, FrameKind.FLAT),
    "darkflat": (ImageType.DARKFLAT, FrameKind.DARK),
    "dark": (ImageType.DARK, FrameKind.DARK),
}
#: Close enough to the set point to start shooting.
TEMP_TOLERANCE_C = 1.0
COOLING_TIMEOUT_S = 20 * 60.0


class SessionGroupTask(CaptureSequenceTask):
    kind = "session_group"

    def __init__(
        self,
        observatory: Observatory,
        group: str,
        *,
        session_id: str | None = None,
        settings: dict | None = None,
        into_library: bool = False,
    ) -> None:
        """A session's group by `session_id`, or library darks from `settings`."""
        if group not in TYPES:
            raise AstropiError(f"no frame group {group!r}")
        if into_library and group != "dark":
            raise AstropiError("only darks go into the library")
        session = None
        if session_id is not None:
            session = observatory.imaging.load(session_id)
            if session is None:
                raise AstropiError(f"no session {session_id!r}")
            settings = session["groups"][group]
        elif not into_library or settings is None:
            raise AstropiError("frames outside a session can only be library darks")
        self._session_id = session_id
        self._group = group
        self._into_library = into_library
        self._image_type, frame_kind = TYPES[group]
        where = " into the library" if into_library else ""
        super().__init__(
            observatory,
            CapturePlan(
                count=int(settings["count"]),
                exposure_s=float(settings["exposure_s"] or 0.0),
                gain=settings["gain"],
                offset=settings["offset"],
                binning=int(settings.get("binning", 1)),
                kind=frame_kind,
                dither_every=int(settings.get("dither_every", 0)) if group == "light" else 0,
                name=f"{session['target_name']}: {LABELS[group]}{where}" if session else "Library darks",
            ),
        )
        self._settings = settings
        self._directory = None
        self._filter: str | None = None
        self._base = 0
        self._sensor_temps: list[float] = []

    async def run(self):
        observatory = self._observatory
        # Which group this is, for the panel that shows its progress.
        self.report(
            "starting",
            session_id=self._session_id,
            group=self._group,
            into_library=self._into_library,
        )
        if self._session_id is not None:
            session = observatory.current_session = self._session()
            self._filter = group_filter(session, self._group)
            self._write_metadata(session)
            if self._group == "flat" and self._filter != group_filter(session, "light"):
                self.report(
                    "starting",
                    message=(
                        f"Warning: flats through {self._filter}, lights through "
                        f"{group_filter(session, 'light')} - they will not calibrate them"
                    ),
                )
        settings = self._settings
        temp_c = settings.get("temp_c")
        if self._into_library and temp_c is None:
            raise AstropiError("library darks need a set-point temperature to be matched by")
        if temp_c is not None:
            await self._cool(float(temp_c))

        if self._group == "flat" and (settings.get("auto_exposure") or not settings.get("exposure_s")):
            exposure = await find_flat_exposure(
                observatory.camera(),
                lambda message, **detail: self.report("finding exposure", message=message, **detail),
                gain=self._plan.gain,
                offset=self._plan.offset,
                binning=self._plan.binning,
                start_s=float(settings.get("exposure_s") or 1.0),
            )
            if exposure < FLICKER_WARN_S:
                self.report(
                    "finding exposure",
                    message=(
                        f"Flats at {exposure:g}s are short enough for panel flicker to show - "
                        "dim the panel for a longer exposure if they look uneven"
                    ),
                )
            self._plan.exposure_s = exposure
            # Remembered on the session, and passed on to the dark flats
            # if they still follow the flats.
            session = self._session()
            session["groups"]["flat"]["exposure_s"] = exposure
            observatory.imaging.save(relink(session))
        if self._plan.exposure_s <= 0:
            raise AstropiError(f"set an exposure for the {LABELS[self._group].lower()} first")

        if self._into_library:
            self._directory = observatory.dark_library.folder_for(
                self._plan.exposure_s, self._plan.gain, self._plan.offset, float(temp_c)
            )
        else:
            # Start again where a stopped run left off; a finished group
            # starts over.
            done = int(settings.get("captured", 0))
            if 0 < done < self._plan.count:
                self._base = done
                self._plan.count -= done
                self.report("resuming", message=f"{done} already shot, {self._plan.count} to go")
            else:
                self._update(captured=0)
        try:
            return await super().run()
        finally:
            if self._into_library and self._directory is not None and self._directory.exists():
                observatory.dark_library.record(
                    self._directory,
                    exposure_s=self._plan.exposure_s,
                    gain=self._plan.gain,
                    offset=self._plan.offset,
                    temp_c=float(temp_c),
                    camera=observatory.camera().descriptor.name,
                    sensor_temps=self._sensor_temps,
                )

    async def _save(self, frame, index: int) -> str:
        session = self._session() if self._session_id is not None else {}
        if frame.metadata.get("sensor_temp_c") is not None:
            self._sensor_temps.append(float(frame.metadata["sensor_temp_c"]))
        path = await self._observatory.save_capture(
            frame,
            image_type=self._image_type,
            target_name=session.get("target_name"),
            night=session.get("night"),
            directory=self._directory,
            temp_c=self._settings.get("temp_c"),
            filter_name=self._filter,
        )
        if not self._into_library:
            self._update(captured=self._base + index)
        return path

    async def _cool(self, target_c: float) -> None:
        """Bring the sensor to the set point, and wait until it is there."""
        camera = self._observatory.camera()
        try:
            await camera.set_cooling(True, target_c)
        except CapabilityError:
            self.report("cooling", message="This camera has no cooler; shooting at whatever it is")
            return
        started = time.monotonic()
        polls = 0
        while True:
            sensor = (await camera.status()).cooling.sensor_c
            if sensor is not None and abs(sensor - target_c) <= TEMP_TOLERANCE_C:
                self.report("cooling", message=f"Sensor at {sensor:.1f}C, set point {target_c:g}C")
                return
            if time.monotonic() - started > COOLING_TIMEOUT_S:
                raise AstropiError(
                    f"the sensor did not reach {target_c:g}C in {COOLING_TIMEOUT_S / 60:.0f} minutes "
                    f"(it is at {sensor}C) - is the cooler powered, or the set point too low for tonight?"
                )
            # A line every half minute; the reading itself every poll.
            message = f"Cooling to {target_c:g}C, sensor at {sensor}C" if polls % 6 == 0 else None
            self.report("cooling", message=message, sensor_c=sensor, target_c=target_c)
            polls += 1
            await asyncio.sleep(5.0)

    def _write_metadata(self, session: dict) -> None:
        """The session's settings beside its frames, for whoever stacks them."""
        try:
            self._observatory.archive.check()
            folder = self._observatory.archive.root / session_folder(session["night"], session["target_name"])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "session.json").write_text(json.dumps(session, indent=2))
        except Exception:
            logger.warning("could not write the session metadata", exc_info=True)

    def _session(self) -> dict:
        session = self._observatory.imaging.load(self._session_id)
        if session is None:
            raise AstropiError("the session was deleted")
        return session

    def _update(self, **values) -> None:
        session = self._session()
        session["groups"][self._group].update(values)
        self._observatory.imaging.save(session)
        if self._observatory.current_session and self._observatory.current_session["id"] == session["id"]:
            self._observatory.current_session = session
