"""The Observatory: one object that owns the whole running rig.

Everything reachable from here - devices, services, the task engine - is
constructed in `build`, which is the only place that knows which backend is
in use. Route handlers, tasks and the WebSocket all take an `Observatory`
and ask it for what they need, so none of them ever import a driver.

Switching from the simulator to real hardware means adding a branch in
`_build_devices` and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from astropi.config import Backend, Settings, load_settings
from astropi.core.errors import DeviceNotFoundError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import RaDec, format_dms, format_hms
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import hour_angle_deg
from astropi.devices import (
    CameraDevice,
    DeviceRegistry,
    DeviceRole,
    Focuser,
    Mount,
)
from astropi.devices.backends.simulator import (
    SimulatedCamera,
    SimulatedCameraConfig,
    SimulatedFocuser,
    SimulatedMount,
    SimulatedMountConfig,
    guide_camera_config,
)
from astropi.sequencing.task import TaskEngine
from astropi.services.catalog import CatalogService, Target, TargetSource
from astropi.services.ephemeris import EphemerisService
from astropi.services.guiding import GuidingService
from astropi.services.planning import PlannerService
from astropi.services.platesolve import (
    AstapSolver,
    AstrometryNetSolver,
    PlateSolveService,
    SimulatedPlateSolver,
)
from astropi.services.polaralign import PolarAlignmentService
from astropi.storage import FrameStore
from astropi.storage.sessions import SessionStore

logger = logging.getLogger(__name__)


class Observatory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.events = EventBus()
        self.site = ObservingSite(
            latitude_deg=settings.latitude_deg,
            longitude_deg=settings.longitude_deg,
            elevation_m=settings.elevation_m,
            name=settings.site_name,
        )

        self.registry = DeviceRegistry(self.events)
        self.ephemeris = EphemerisService(self.site)
        self.catalog = CatalogService(self.ephemeris, settings.catalog_dir)
        self.frames = FrameStore(capacity=settings.frame_cache_size)
        self.tasks = TaskEngine(self.events)
        self.polar_alignment = PolarAlignmentService(self.site, self.events)
        self.planner = PlannerService(self.ephemeris)
        self.sessions = SessionStore(settings.data_dir / "sessions")
        self.plate_solver = PlateSolveService(self._build_solvers(), self.events)
        self.guider: GuidingService | None = None
        #: What the rig is working on. Session state rather than device
        #: state: the mount knows a coordinate, not a name.
        self.active_target: Target | None = None

    # ------------------------------------------------------------ lifecycle

    @classmethod
    async def build(cls, settings: Settings | None = None) -> Observatory:
        observatory = cls(settings or load_settings())
        await observatory._build_devices()
        await observatory.registry.connect_all()
        observatory._build_guider()
        return observatory

    async def shutdown(self) -> None:
        if self.guider is not None:
            await self.guider.stop()
        await self.tasks.cancel()
        await self.registry.disconnect_all()

    def _build_solvers(self) -> list:
        """Solvers in preference order: local and fast before remote."""
        solvers: list[Any] = [AstapSolver(self.settings.astap_binary)]
        if self.settings.astrometry_api_key:
            solvers.append(AstrometryNetSolver(self.settings.astrometry_api_key))
        if self.settings.backend is Backend.SIMULATOR:
            # Last, so a real ASTAP install is still preferred if present.
            solvers.append(SimulatedPlateSolver(duration_s=self.settings.simulator_solve_seconds))
        return solvers

    async def _build_devices(self) -> None:
        if self.settings.backend is not Backend.SIMULATOR:
            raise NotImplementedError(
                f"the {self.settings.backend} backend is not implemented yet. "
                "Write an adapter under astropi/devices/backends/ that satisfies "
                "the Mount, Camera and Focuser protocols, then register it here; "
                "nothing above this layer needs to change."
            )

        settings = self.settings
        mount = SimulatedMount(
            self.site,
            self.events,
            SimulatedMountConfig(
                slew_rate_deg_per_s=settings.simulator_slew_rate_deg_per_s,
                time_scale=settings.simulator_time_scale,
            ),
        )
        focuser = SimulatedFocuser()

        camera_config = SimulatedCameraConfig(
            width=settings.camera_width,
            height=settings.camera_height,
            pixel_size_um=settings.camera_pixel_size_um,
            focal_length_mm=settings.focal_length_mm,
            rotation_deg=settings.camera_rotation_deg,
            catalog=self.catalog.as_star_array(),
            time_scale=settings.simulator_time_scale,
        )
        camera = SimulatedCamera(mount, self.events, camera_config, focuser=focuser)
        guide_camera = SimulatedCamera(
            mount, self.events, guide_camera_config(camera_config), focuser=focuser
        )

        self.registry.register(DeviceRole.MOUNT, mount)
        self.registry.register(DeviceRole.CAMERA, camera)
        self.registry.register(DeviceRole.GUIDE_CAMERA, guide_camera)
        self.registry.register(DeviceRole.FOCUSER, focuser)

    def _build_guider(self) -> None:
        """Wire the guide loop, if there is a guide camera to run it with."""
        if not self.registry.has(DeviceRole.GUIDE_CAMERA) or not self.registry.has(DeviceRole.MOUNT):
            logger.info("no guide camera registered; guiding unavailable")
            return

        guide_camera = self.registry.get(DeviceRole.GUIDE_CAMERA, CameraDevice)
        self.guider = GuidingService(
            camera=guide_camera,
            mount=self.mount(),
            events=self.events,
            pixel_scale_arcsec=self.guide_pixel_scale_arcsec(),
        )

    # --------------------------------------------------------------- target

    def set_active_target(self, target: Target | None) -> None:
        """Record what the rig is pointed at, and tell everyone."""
        self.active_target = target
        if target is None:
            self.events.publish(Topic.TARGET, target=None)
            return
        self.events.publish(Topic.TARGET, target=self.describe_target(target))

    def target_for_coord(self, coord: RaDec, name: str | None = None) -> Target:
        """Wrap a bare coordinate as a target, for a GoTo with no catalogue entry."""
        return Target(
            id="custom",
            name=name or str(coord),
            coord=coord,
            source=TargetSource.CUSTOM,
            object_type="Coordinates",
        )

    def describe_target(self, target: Target) -> dict[str, Any]:
        """Serialise a target with its altitude now, for the API and socket."""
        altitude, azimuth = self.ephemeris.altaz_now(target.coord)
        return {
            "id": target.id,
            "name": target.name,
            "display_name": target.display_name,
            "object_type": target.object_type,
            "source": str(target.source),
            "magnitude": target.magnitude if target.magnitude < 90 else None,
            "ra_deg": target.coord.ra_deg,
            "dec_deg": target.coord.dec_deg,
            "ra_hms": format_hms(target.coord.ra_deg),
            "dec_dms": format_dms(target.coord.dec_deg),
            "altitude_deg": round(altitude, 2),
            "azimuth_deg": round(azimuth, 2),
            "hour_angle_deg": round(hour_angle_deg(target.coord.ra_deg, self.site.longitude_deg), 4),
        }

    # ------------------------------------------------------------- accessors

    def mount(self) -> Mount:
        return self.registry.require_connected(DeviceRole.MOUNT, Mount)

    def camera(self) -> CameraDevice:
        return self.registry.require_connected(DeviceRole.CAMERA, CameraDevice)

    def guide_camera(self) -> CameraDevice:
        return self.registry.require_connected(DeviceRole.GUIDE_CAMERA, CameraDevice)

    def focuser(self) -> Focuser:
        if not self.registry.has(DeviceRole.FOCUSER):
            raise DeviceNotFoundError("no focuser is connected; autofocus is unavailable")
        return self.registry.require_connected(DeviceRole.FOCUSER, Focuser)

    def require_guider(self) -> GuidingService:
        if self.guider is None:
            raise DeviceNotFoundError("guiding is unavailable without a guide camera")
        return self.guider

    # ------------------------------------------------------------ properties

    def pixel_scale_arcsec(self, role: DeviceRole = DeviceRole.CAMERA) -> float:
        """Arcseconds per pixel for a sensor on the configured optics."""
        camera = self.registry.get(role, CameraDevice)
        sensor = camera.sensor
        focal_length = self.settings.focal_length_mm
        if role is DeviceRole.GUIDE_CAMERA and self.settings.guide_focal_length_mm:
            focal_length = self.settings.guide_focal_length_mm
        return 206.264806 * sensor.pixel_size_um / focal_length

    def guide_pixel_scale_arcsec(self) -> float:
        return self.pixel_scale_arcsec(DeviceRole.GUIDE_CAMERA)

    def set_site(self, site: ObservingSite) -> None:
        """Move the observing site, refreshing everything derived from it."""
        self.site = site
        self.ephemeris = EphemerisService(site)
        self.catalog.set_ephemeris(self.ephemeris)
        self.planner = PlannerService(self.ephemeris)
        self.polar_alignment = PolarAlignmentService(site, self.events)

    def describe(self) -> dict[str, Any]:
        camera_scale = None
        if self.registry.has(DeviceRole.CAMERA):
            camera_scale = round(self.pixel_scale_arcsec(), 3)
        return {
            "backend": str(self.settings.backend),
            "site": {
                "name": self.site.name,
                "latitude_deg": self.site.latitude_deg,
                "longitude_deg": self.site.longitude_deg,
                "elevation_m": self.site.elevation_m,
            },
            "optics": {
                "focal_length_mm": self.settings.focal_length_mm,
                "pixel_scale_arcsec": camera_scale,
            },
            "catalog_size": len(self.catalog),
            "devices": {
                str(role): {
                    "id": d.id,
                    "name": d.name,
                    "driver": d.driver,
                    "capabilities": sorted(str(c) for c in d.capabilities),
                }
                for role, d in self.registry.descriptors().items()
            },
        }
