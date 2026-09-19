"""Polar alignment must recover an error the simulator was given.

This is a genuine inversion, not a lookup: the simulator applies a tilted
rotation axis and produces frames, and the routine recovers that axis from
nothing but three plate-solved positions.
"""

from __future__ import annotations

import pytest

from astropi.core.events import EventBus
from astropi.devices.backends.simulator import (
    SimulatedCamera,
    SimulatedCameraConfig,
    SimulatedMount,
    SimulatedMountConfig,
)
from astropi.devices.camera import ExposureRequest
from astropi.services.platesolve import SimulatedPlateSolver
from astropi.services.polaralign import PolarAlignmentService, measure


async def _rig(site, alt_error: float, az_error: float):
    events = EventBus()
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            polar_alt_error_deg=alt_error,
            polar_az_error_deg=az_error,
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.2,
        ),
        seed=7,
    )
    camera = SimulatedCamera(
        mount, events, SimulatedCameraConfig(width=2000, height=1500, time_scale=0.01, readout_s=0.0), seed=7
    )
    await mount.connect()
    await camera.connect()
    await mount.unpark()
    return mount, camera, PolarAlignmentService(site, events)


def _drivers(mount, camera, solver):
    async def slew(target):
        await mount.slew_to(target)
        await mount.wait_for_slew()

    async def solve():
        frame = await camera.expose(ExposureRequest(duration_s=4.0))
        return (await solver.solve(frame)).center

    return slew, solve


@pytest.mark.parametrize(
    ("alt_error", "az_error"),
    [(0.35, -0.22), (-0.80, 1.10), (0.02, 0.01)],
)
async def test_recovers_the_simulated_polar_error(site, alt_error, az_error):
    mount, camera, service = await _rig(site, alt_error, az_error)
    solver = SimulatedPlateSolver(seed=7, duration_s=0.0)
    slew, solve = _drivers(mount, camera, solver)

    result = await measure(service, slew, solve, points=3)

    assert result.altitude_error_deg == pytest.approx(alt_error, abs=0.03)
    assert result.azimuth_error_deg == pytest.approx(az_error, abs=0.03)


async def test_instructions_name_the_right_knob_and_direction(site):
    mount, camera, service = await _rig(site, 0.5, -0.4)
    solver = SimulatedPlateSolver(seed=7, duration_s=0.0)
    slew, solve = _drivers(mount, camera, solver)

    result = await measure(service, slew, solve, points=3)
    instructions = " ".join(result.instructions())

    # Polar axis too high and rotated west of the pole.
    assert "Altitude: move down" in instructions
    assert "Azimuth: move east" in instructions


async def test_refinement_tracks_the_knobs(site):
    """Live feedback must follow the error down as it is adjusted out."""
    mount, camera, service = await _rig(site, 0.35, -0.22)
    solver = SimulatedPlateSolver(seed=7, duration_s=0.0)
    slew, solve = _drivers(mount, camera, solver)
    await measure(service, slew, solve, points=3)

    for alt_error, az_error in [(0.20, -0.12), (0.05, -0.03), (0.005, 0.002)]:
        mount.set_polar_error(alt_error, az_error)
        refined = service.refine(await solve())
        assert refined.altitude_error_deg == pytest.approx(alt_error, abs=0.03)
        assert refined.azimuth_error_deg == pytest.approx(az_error, abs=0.03)


async def test_three_collinear_points_are_rejected(site):
    """A sweep too short to define a plane must fail loudly, not guess."""
    from astropi.core.errors import AstropiError
    from astropi.core.geometry import RaDec

    _, _, service = await _rig(site, 0.3, 0.3)
    for _ in range(3):
        service.record(RaDec(100.0, 30.0))
    with pytest.raises(AstropiError, match="collinear"):
        service.compute()
