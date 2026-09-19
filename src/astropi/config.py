"""Settings, from environment variables or a .env file.

Every value has a default that works on a laptop with no hardware, so a
fresh clone runs without configuration. Moving to the Pi is a matter of
switching `backend` and filling in the optical train.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Backend(StrEnum):
    SIMULATOR = "simulator"
    INDI = "indi"
    ALPACA = "alpaca"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASTROPI_", env_file=".env", extra="ignore")

    backend: Backend = Backend.SIMULATOR
    host: str = "0.0.0.0"
    port: int = 8000

    # Observing site. Defaults to Paris so the catalogue and ephemeris
    # return something sensible before anyone sets a real location.
    latitude_deg: float = 48.8566
    longitude_deg: float = 2.3522
    elevation_m: float = 35.0
    site_name: str = "Home"

    # Optical train. Pixel scale and field of view follow from these, and
    # both plate solving and guiding need them to be right.
    focal_length_mm: float = 400.0
    camera_pixel_size_um: float = 3.76
    camera_width: int = 6248
    camera_height: int = 4176
    camera_rotation_deg: float = 0.0

    guide_focal_length_mm: float | None = None

    # INDI, when the backend is switched to it.
    indi_host: str = "localhost"
    indi_port: int = 7624

    # Simulator tuning. Lets a demo or a test run a night's sequence in
    # seconds without changing any of the geometry being exercised.
    simulator_time_scale: float = 1.0
    simulator_slew_rate_deg_per_s: float = 4.0
    simulator_solve_seconds: float = 0.4

    astap_binary: str = "astap"
    astrometry_api_key: str | None = None

    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    #: Frames held in memory for preview. Full ASI2600 frames are ~52 MB.
    frame_cache_size: int = 12

    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def catalog_dir(self) -> Path:
        return self.data_dir / "catalog"


def load_settings() -> Settings:
    return Settings()
