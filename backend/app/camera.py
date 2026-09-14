from dataclasses import dataclass
from typing import Protocol

from .mount import RaDec


@dataclass
class Frame:
    # Placeholder for real frame bytes/path once gphoto2 capture is wired in.
    # Carries the true sky position it was taken at, so MockPlateSolver has
    # something to "solve" against without real image analysis.
    true_position: RaDec


class Camera(Protocol):
    def capture(self) -> Frame: ...


class MockCamera:
    """Stands in for gphoto2 tethered capture. Takes a snapshot of wherever
    the mount is currently actually pointed (its `position()`), simulating
    a real photo of the sky at that spot.
    """

    def __init__(self, mount) -> None:
        self._mount = mount

    def capture(self) -> Frame:
        return Frame(true_position=self._mount.position())
