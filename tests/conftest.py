from __future__ import annotations

import pytest

from astropi.config import Settings
from astropi.core.events import EventBus
from astropi.core.site import ObservingSite


@pytest.fixture
def site() -> ObservingSite:
    return ObservingSite(latitude_deg=48.8566, longitude_deg=2.3522, name="Test site")


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def settings() -> Settings:
    """A small sensor, so tests render frames in milliseconds.

    The real ASI2600MC frame is 26 megapixels; rendering one per exposure
    would make the suite take minutes for no extra coverage.
    """
    return Settings(
        camera_width=1600,
        camera_height=1200,
        latitude_deg=48.8566,
        longitude_deg=2.3522,
    )
