"""A complete rig you can develop against with no hardware attached."""

from astropi.devices.backends.simulator.camera import (
    SimulatedCamera,
    SimulatedCameraConfig,
    guide_camera_config,
)
from astropi.devices.backends.simulator.focuser import SimulatedFocuser, SimulatedFocuserConfig
from astropi.devices.backends.simulator.mount import SimulatedMount, SimulatedMountConfig
from astropi.devices.backends.simulator.sky import OpticalTrain

__all__ = [
    "OpticalTrain",
    "SimulatedCamera",
    "SimulatedCameraConfig",
    "SimulatedFocuser",
    "SimulatedFocuserConfig",
    "SimulatedMount",
    "SimulatedMountConfig",
    "guide_camera_config",
]
