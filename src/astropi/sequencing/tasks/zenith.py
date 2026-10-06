"""Point straight up, for flats.

A flat panel or a t-shirt sits evenly on a level tube, and the zenith is
the one point of the sky that is the same for every night: hour angle
zero, declination equal to the site's latitude.

Hour angle zero is the meridian, where a German mount normally flips. The
mount keeps the side it is already on for a target this close to the
meridian, so the move is the short one with no flip. Tracking is stopped
on arrival: a tracking mount would carry the scope off the zenith, and the
panel with it.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError
from astropi.core.geometry import RaDec
from astropi.core.timekeeping import local_sidereal_time_deg
from astropi.devices.mount import MountState
from astropi.sequencing.task import Task

if TYPE_CHECKING:
    from astropi.runtime import Observatory

#: Arriving lower than this means the slew was stopped on the way.
ARRIVED_ALTITUDE_DEG = 85.0


def zenith(latitude_deg: float, longitude_deg: float, when: float | None = None) -> RaDec:
    return RaDec(local_sidereal_time_deg(longitude_deg, when) % 360.0, latitude_deg)


class ZenithTask(Task):
    kind = "zenith"

    def __init__(self, observatory: Observatory) -> None:
        super().__init__(name="Point straight up")
        self._observatory = observatory

    async def run(self) -> dict:
        observatory = self._observatory
        mount = observatory.mount()
        before = await mount.status()
        if before.state is MountState.PARKED:
            await mount.unpark()
        site = observatory.site
        target = zenith(site.latitude_deg, site.longitude_deg, time.time())
        self.report(
            "slewing",
            fraction=0.0,
            message=f"Slewing to the zenith (HA 0, Dec {site.latitude_deg:+.1f}), pier side kept",
            target_ra_deg=target.ra_deg,
            target_dec_deg=target.dec_deg,
        )
        # Pointing at nothing in the catalogue: lights must not be filed
        # under whatever was framed before.
        observatory.set_active_target(None)
        try:
            await mount.slew_to(target)
            await mount.wait_for_slew()
        except asyncio.CancelledError:
            # Cancelling the task has to stop the motors, not just the waiting.
            await mount.abort_slew()
            raise
        await mount.set_tracking(False)

        status = await mount.status()
        altitude = status.horizontal.alt_deg if status.horizontal else None
        if altitude is not None and altitude < ARRIVED_ALTITUDE_DEG:
            raise AstropiError(f"stopped at altitude {altitude:.0f} degrees, short of the zenith")
        self.report(
            "done",
            fraction=1.0,
            message=(
                "Pointing straight up, tracking off"
                + (f" (altitude {altitude:.1f}, pier {status.pier_side})" if altitude is not None else "")
            ),
        )
        return {"altitude_deg": altitude, "pier_side": str(status.pier_side)}
