"""Request and response models.

Separate from the domain dataclasses on purpose. The wire format is a
contract with the browser and should be free to stay still while internals
move; and these are what generate the OpenAPI schema the frontend's types
are built from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from astropi.core.geometry import RaDec, format_dms, format_hms, parse_angle


class CoordinateIn(BaseModel):
    """A target coordinate, accepting degrees or sexagesimal text.

    Sexagesimal is accepted because that is how catalogues, planetarium
    apps and forum posts all write coordinates; forcing decimal degrees
    would make the common case a manual conversion.
    """

    ra_deg: float | None = None
    dec_deg: float | None = None
    ra: str | None = Field(default=None, description="e.g. '00h42m44s' or '10.6847'")
    dec: str | None = Field(default=None, description="e.g. '+41d16m09s' or '41.269'")

    def to_radec(self) -> RaDec:
        ra = self.ra_deg if self.ra_deg is not None else parse_angle(self.ra or "", hours=True)
        dec = self.dec_deg if self.dec_deg is not None else parse_angle(self.dec or "")
        return RaDec(ra, dec).normalized()


class CoordinateOut(BaseModel):
    ra_deg: float
    dec_deg: float
    ra_hms: str
    dec_dms: str

    @classmethod
    def of(cls, coord: RaDec) -> CoordinateOut:
        return cls(
            ra_deg=round(coord.ra_deg, 6),
            dec_deg=round(coord.dec_deg, 6),
            ra_hms=format_hms(coord.ra_deg),
            dec_dms=format_dms(coord.dec_deg),
        )


class SiteIn(BaseModel):
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)
    elevation_m: float = 0.0
    name: str = "Home"


class SiteOut(SiteIn):
    hemisphere: str


class TargetOut(BaseModel):
    id: str
    name: str
    display_name: str
    object_type: str
    source: str
    magnitude: float
    constellation: str | None = None
    common_names: list[str] = Field(default_factory=list)
    coord: CoordinateOut
    altitude_deg: float | None = None


class VisibilityOut(BaseModel):
    altitude_now_deg: float
    azimuth_now_deg: float
    max_altitude_deg: float
    transit_at: datetime | None
    rises_at: datetime | None
    sets_at: datetime | None
    circumpolar: bool
    never_rises: bool
    hours_above_horizon: float
    moon_separation_deg: float
    curve: list[dict[str, Any]]


class NightOut(BaseModel):
    sunset: datetime | None
    sunrise: datetime | None
    astronomical_dusk: datetime | None
    astronomical_dawn: datetime | None
    dark_hours: float
    moon_illumination: float
    moon_altitude_deg: float


class MountOut(BaseModel):
    state: str
    tracking: bool
    tracking_rate: str
    pier_side: str
    slewing: bool
    coord: CoordinateOut
    altitude_deg: float | None = None
    azimuth_deg: float | None = None
    target: CoordinateOut | None = None
    #: Negative is east of the meridian, positive west. Fifteen degrees an hour.
    hour_angle_deg: float | None = None


class TrackingIn(BaseModel):
    enabled: bool
    rate: Literal["sidereal", "lunar", "solar", "king"] = "sidereal"


class PulseGuideIn(BaseModel):
    """The guiding primitive: a correction, in milliseconds at guide rate.

    Not the control for framing, which is `NudgeIn` below. At half
    sidereal this moves about seven arcseconds a second, so a full minute
    of it travels an eighth of a degree - correct for a closed loop
    chasing a star, and indistinguishable from a dead mount to anyone
    watching one.
    """

    direction: Literal["north", "south", "east", "west"]
    duration_ms: int = Field(gt=0, le=60_000)


class NudgeIn(BaseModel):
    """A framing move: one axis, one angle, at whatever speed the mount has.

    An angle rather than a duration because an angle is the thing being
    asked for. "Half a degree east" survives being moved to a mount that
    slews at a different speed; "two seconds east" does not.
    """

    direction: Literal["north", "south", "east", "west"]
    #: Bigger than this is a GoTo, and should be one.
    degrees: float = Field(gt=0, le=45.0)


class ExposureIn(BaseModel):
    duration_s: float = Field(gt=0, le=3600)
    gain: int | None = None
    offset: int | None = None
    binning: int = Field(default=1, ge=1, le=8)
    kind: Literal["light", "dark", "flat", "bias", "preview", "guide"] = "preview"


class CoolingIn(BaseModel):
    enabled: bool
    target_c: float | None = None


class CameraOut(BaseModel):
    state: str
    gain: int | None
    offset: int | None
    binning: int
    exposure_progress: float | None
    sensor: dict[str, Any]
    cooling: dict[str, Any]
    pixel_scale_arcsec: float
    field_of_view_deg: list[float]


class ControlOut(BaseModel):
    """One camera control, as the driver describes it.

    Carries its own limits and its own writability so the client needs no
    table of its own: this is what lets one panel drive a cooled colour
    main camera and an uncooled mono guide sensor.
    """

    name: str
    label: str
    value: float | None
    writable: bool
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    step: float = 1.0
    unit: str | None = None
    supports_auto: bool = False
    auto: bool = False
    description: str | None = None


class GuidingOut(BaseModel):
    state: str
    calibrated: bool
    rms_ra_arcsec: float | None
    rms_dec_arcsec: float | None
    rms_total_arcsec: float | None
    samples: int
    calibration: dict[str, Any] | None = None


class DitherIn(BaseModel):
    amount_px: float = Field(default=12.0, gt=0, le=200)
    settle_px: float = Field(default=1.5, gt=0)
    settle_time_s: float = Field(default=8.0, ge=0, le=300)


class GotoIn(BaseModel):
    """Either a catalogue target or an explicit coordinate."""

    target_id: str | None = None
    coord: CoordinateIn | None = None
    center: bool = Field(default=True, description="Plate-solve and correct after slewing")
    tolerance_arcmin: float = Field(default=1.0, gt=0, le=60)
    max_iterations: int = Field(default=5, ge=1, le=10)
    exposure_s: float = Field(default=4.0, gt=0, le=120)


class PolarAlignIn(BaseModel):
    points: int = Field(default=3, ge=3, le=8)
    separation_deg: float = Field(default=25.0, ge=5, le=90)
    exposure_s: float = Field(default=4.0, gt=0, le=60)


class AutofocusIn(BaseModel):
    steps: int = Field(default=9, ge=3, le=25)
    step_size: int = Field(default=350, ge=1)
    exposure_s: float = Field(default=3.0, gt=0, le=60)


class CaptureIn(BaseModel):
    count: int = Field(ge=1, le=10_000)
    exposure_s: float = Field(gt=0, le=3600)
    gain: int | None = None
    offset: int | None = None
    binning: int = Field(default=1, ge=1, le=8)
    dither_every: int = Field(default=3, ge=0, le=100)
    dither_px: float = Field(default=12.0, gt=0)
    name: str = "Light frames"


class TaskOut(BaseModel):
    id: str
    kind: str
    name: str
    state: str
    step: str
    fraction: float | None
    detail: dict[str, Any]
    messages: list[str]
    error: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
