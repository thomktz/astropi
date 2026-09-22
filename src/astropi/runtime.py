"""The Observatory: one object that owns the whole running rig.

Everything reachable from here - devices, services, the task engine - is
constructed in `build`, which is the only place that knows which backend is
in use. Route handlers, tasks and the WebSocket all take an `Observatory`
and ask it for what they need, so none of them ever import a driver.

Which mount is driving - the simulator or the one on the end of a USB
cable - is decided here too, and can be changed while the rig is running:
`switch_mount` swaps the device in the registry, rewires the things that
hold a reference to it, and remembers the choice for next time.
"""

from __future__ import annotations

import logging
from typing import Any

from astropi.config import Backend, MountDriver, Settings, load_settings
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
from astropi.devices.backends.synta.mount import SyntaMount, SyntaMountConfig
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
from astropi.services.preview import PreviewService
from astropi.storage import FrameStore
from astropi.storage.sessions import SessionStore
from astropi.storage.state import StateStore

logger = logging.getLogger(__name__)


class Observatory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.events = EventBus()
        # Settings describe the machine; the state file describes the
        # evening - where you are standing, and which mount you are
        # driving. Both were being set in the dashboard and forgotten on
        # every restart.
        self.state = StateStore(settings.data_dir)

        stored_site = self.state.get("site")
        self.site = ObservingSite(
            latitude_deg=stored_site.get("latitude_deg", settings.latitude_deg),
            longitude_deg=stored_site.get("longitude_deg", settings.longitude_deg),
            elevation_m=stored_site.get("elevation_m", settings.elevation_m),
            name=stored_site.get("name", settings.site_name),
        )

        stored_mount = self.state.get("mount")
        self.mount_driver = MountDriver(stored_mount.get("driver", settings.mount_driver))
        self.mount_port = stored_mount.get("port", settings.mount_port)
        #: Tuned against the rig it is driving, not the code: a mount that
        #: skips steps at one speed is fine at half of it, and which speed
        #: that is depends on the payload and the night.
        self.mount_slew_rate = float(stored_mount.get("slew_rate", settings.mount_slew_rate))

        self.registry = DeviceRegistry(self.events)
        self.ephemeris = EphemerisService(self.site)
        self.catalog = CatalogService(self.ephemeris, settings.catalog_dir)
        self.frames = FrameStore(capacity=settings.frame_cache_size)
        # The guard stands the live view down for the length of a task,
        # so a centring exposure never collides with a preview frame.
        self.tasks = TaskEngine(
            self.events,
            camera_guard=lambda: self.require_preview().paused(),
        )
        self.polar_alignment = PolarAlignmentService(self.site, self.events)
        self.planner = PlannerService(self.ephemeris)
        self.sessions = SessionStore(settings.data_dir / "sessions")
        self.plate_solver = PlateSolveService(self._build_solvers(), self.events)
        self.guider: GuidingService | None = None
        self.preview: PreviewService | None = None
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
        await observatory._start_preview()
        if observatory.guider is not None:
            # The guide view is live from boot too, for the same reason the
            # main one is: a dark sub-display tells you nothing about
            # whether the guide sensor is focused or clouded over.
            await observatory.guider.start_preview()
        return observatory

    async def shutdown(self) -> None:
        if self.preview is not None:
            await self.preview.stop()
        if self.guider is not None:
            await self.guider.stop_preview()
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
        mount = self._new_mount()
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
        # The simulated sensors render whatever the mount says it is
        # pointing at, real mount included - which is how a camera that
        # does not exist yet can still be used to test a mount that does.
        camera = SimulatedCamera(mount, self.events, camera_config, focuser=focuser)
        guide_camera = SimulatedCamera(
            mount, self.events, guide_camera_config(camera_config), focuser=focuser
        )
        self._cameras = (camera, guide_camera)

        self.registry.register(DeviceRole.MOUNT, mount)
        self.registry.register(DeviceRole.CAMERA, camera)
        self.registry.register(DeviceRole.GUIDE_CAMERA, guide_camera)
        self.registry.register(DeviceRole.FOCUSER, focuser)

    def _new_mount(self) -> Mount:
        """The mount the rig is currently set to drive.

        Both branches satisfy the same protocol, which is the whole reason
        a real mount can be dropped in beside a simulated camera without
        anything above this line noticing.
        """
        if self.mount_driver is MountDriver.SYNTA:
            return SyntaMount(
                self.site,
                self.events,
                SyntaMountConfig(
                    port=self.mount_port,
                    min_altitude_deg=self.settings.mount_min_altitude_deg,
                    guide_rate=self.settings.mount_guide_rate,
                    slew_rate=self.mount_slew_rate,
                ),
            )
        return SimulatedMount(
            self.site,
            self.events,
            SimulatedMountConfig(
                slew_rate_deg_per_s=self.settings.simulator_slew_rate_deg_per_s,
                time_scale=self.settings.simulator_time_scale,
            ),
        )

    async def switch_mount(self, driver: MountDriver, port: str | None = None) -> Mount:
        """Change which mount is driving, without a restart.

        Ordered so that a failure leaves the rig on the mount it already
        had: the new one is built and connected *before* the old one is
        let go, and a mount that will not answer raises here with the old
        one still registered and still tracking.
        """
        previous = self.registry.get(DeviceRole.MOUNT, Mount) if self.registry.has(DeviceRole.MOUNT) else None
        was_driver, was_port = self.mount_driver, self.mount_port
        self.mount_driver = driver
        self.mount_port = port or self.mount_port

        candidate = self._new_mount()
        try:
            await candidate.connect()
        except Exception:
            self.mount_driver, self.mount_port = was_driver, was_port
            raise

        if previous is not None and previous is not candidate:
            try:
                # Stops the motors on the way out. A mount abandoned mid
                # slew keeps slewing.
                await previous.disconnect()
            except Exception:
                logger.exception("could not cleanly release the previous mount")

        self.registry.register(DeviceRole.MOUNT, candidate)
        for camera in getattr(self, "_cameras", ()):  # simulated sensors only
            camera.set_pointing_source(candidate)
        # The guider holds its mount rather than looking it up per frame,
        # so it is rebuilt - which drops the calibration, correctly: a
        # different mount has different rates.
        if self.guider is not None:
            await self.guider.stop_preview()
            await self.guider.stop()
        self._build_guider()

        self._remember_mount()
        self.events.publish(
            Topic.DEVICE_STATE,
            role=str(DeviceRole.MOUNT),
            connection=str(candidate.connection_state),
            driver=str(driver),
            name=candidate.descriptor.name,
        )
        logger.info("mount is now %s (%s)", driver, self.mount_port)
        return candidate

    def _remember_mount(self) -> None:
        self.state.put(
            "mount",
            {
                "driver": str(self.mount_driver),
                "port": self.mount_port,
                "slew_rate": self.mount_slew_rate,
            },
        )

    def set_slew_rate(self, multiplier: float) -> None:
        """Change how fast gotos run, and remember it."""
        self.mount_slew_rate = max(1.0, multiplier)
        if self.registry.has(DeviceRole.MOUNT):
            setter = getattr(self.registry.get(DeviceRole.MOUNT, Mount), "set_slew_rate", None)
            if setter is not None:
                setter(self.mount_slew_rate)
        self._remember_mount()

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

    async def _start_preview(self) -> None:
        """Begin the live view, if there is a camera to run it on."""
        if not self.registry.has(DeviceRole.CAMERA):
            return
        self.preview = PreviewService(
            self.registry.get(DeviceRole.CAMERA, CameraDevice),
            self.events,
            # A callable, so this service does not depend on sequencing.
            is_busy=lambda: self.tasks.busy,
        )
        await self.preview.start()

    def require_preview(self) -> PreviewService:
        if self.preview is None:
            raise DeviceNotFoundError("no camera, so no preview")
        return self.preview

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
            "moon_separation_deg": round(self.ephemeris.moon_separation(target.coord), 1),
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
        # The mount needs it too, and more than anything else does: local
        # sidereal time is what turns a right ascension into an hour angle
        # and then into an axis position. A mount left on the default
        # longitude points at the wrong part of the sky by four minutes of
        # RA for every degree of error.
        if self.registry.has(DeviceRole.MOUNT):
            mount = self.registry.get(DeviceRole.MOUNT, Mount)
            setter = getattr(mount, "set_site", None)
            if setter is not None:
                setter(site)
        self.state.put(
            "site",
            {
                "name": site.name,
                "latitude_deg": site.latitude_deg,
                "longitude_deg": site.longitude_deg,
                "elevation_m": site.elevation_m,
            },
        )

    def describe(self) -> dict[str, Any]:
        camera_scale = None
        if self.registry.has(DeviceRole.CAMERA):
            camera_scale = round(self.pixel_scale_arcsec(), 3)
        return {
            "backend": str(self.settings.backend),
            "mount_driver": str(self.mount_driver),
            "mount_port": self.mount_port,
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
