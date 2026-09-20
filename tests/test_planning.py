"""Session plans: duration estimates and feasibility checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astropi.core.geometry import RaDec
from astropi.core.site import ObservingSite
from astropi.services.ephemeris import EphemerisService
from astropi.services.planning import (
    CENTRING_S,
    FRAME_OVERHEAD_S,
    PlanBlock,
    PlannerService,
    SessionPlan,
    Severity,
)

PARIS = ObservingSite(latitude_deg=48.8566, longitude_deg=2.3522)
M31 = RaDec(10.6847, 41.2690)
#: Circumpolar from Paris, so it is up whenever the test happens to run.
POLAR = RaDec(0.0, 85.0)
#: Never rises from Paris.
SOUTHERN = RaDec(0.0, -80.0)


@pytest.fixture(scope="module")
def planner() -> PlannerService:
    return PlannerService(EphemerisService(PARIS))


def _block(coord: RaDec, **kwargs) -> PlanBlock:
    defaults = {"frames": 10, "exposure_s": 60.0, "dither_every": 0, "center": False}
    return PlanBlock(target_id=None, target_name="Test", coord=coord, **{**defaults, **kwargs})


def test_integration_is_shutter_open_time():
    block = _block(M31, frames=30, exposure_s=120.0)
    assert block.integration_s == pytest.approx(3600.0)


def test_duration_includes_the_overheads_integration_does_not():
    """Wall clock is longer than integration, and the gap is real time."""
    block = _block(M31, frames=30, exposure_s=120.0)
    assert block.duration_s == pytest.approx(3600.0 + 30 * FRAME_OVERHEAD_S)

    centred = _block(M31, frames=30, exposure_s=120.0, center=True)
    assert centred.duration_s == pytest.approx(block.duration_s + CENTRING_S)


def test_dithering_costs_time():
    without = _block(M31, frames=30, exposure_s=60.0, dither_every=0)
    with_dither = _block(M31, frames=30, exposure_s=60.0, dither_every=3)
    assert with_dither.duration_s > without.duration_s


def test_blocks_are_laid_out_back_to_back(planner):
    plan = SessionPlan(
        name="Two blocks",
        blocks=[_block(POLAR, frames=10, exposure_s=60.0), _block(POLAR, frames=5, exposure_s=30.0)],
    )
    schedule = planner.schedule(plan)

    assert len(schedule.blocks) == 2
    assert schedule.blocks[0].ends_at == schedule.blocks[1].starts_at
    assert schedule.ends_at == schedule.blocks[-1].ends_at
    assert schedule.duration_s == pytest.approx(plan.duration_s, abs=1.0)


def test_a_target_below_the_horizon_is_a_problem(planner):
    plan = SessionPlan(name="Impossible", blocks=[_block(SOUTHERN, frames=10, exposure_s=60.0)])
    issues = planner.schedule(plan).blocks[0].issues

    assert any(issue.severity is Severity.PROBLEM for issue in issues)
    assert any("horizon" in issue.message for issue in issues)


def test_a_circumpolar_target_is_fine(planner):
    plan = SessionPlan(name="Easy", blocks=[_block(POLAR, frames=10, exposure_s=60.0)])
    block = planner.schedule(plan).blocks[0]

    assert block.min_altitude_deg > 20.0
    assert not [issue for issue in block.issues if issue.severity is Severity.PROBLEM]


def test_altitude_is_sampled_across_the_block_not_just_at_its_ends(planner):
    """A long block can clear both ends and still clip the horizon inside."""
    assert PlannerService.SAMPLES_PER_BLOCK >= 5

    # Twelve hours on one target: the sampled minimum has to be well below
    # the value at either end, which a two-point check would miss.
    plan = SessionPlan(name="All night", blocks=[_block(M31, frames=360, exposure_s=120.0)])
    block = planner.schedule(plan).blocks[0]
    assert block.min_altitude_deg < block.max_altitude_deg


def test_meridian_crossing_is_reported(planner):
    """A twelve-hour block on any target must cross the meridian once."""
    plan = SessionPlan(name="All night", blocks=[_block(M31, frames=360, exposure_s=120.0)])
    assert planner.schedule(plan).blocks[0].crosses_meridian


def test_running_past_dawn_is_flagged(planner):
    """Twenty hours of imaging does not fit in a night."""
    plan = SessionPlan(name="Endless", blocks=[_block(POLAR, frames=600, exposure_s=120.0)])
    schedule = planner.schedule(plan)
    assert any("dawn" in issue.message for issue in schedule.issues)


def test_an_empty_plan_says_so(planner):
    schedule = planner.schedule(SessionPlan(name="Nothing"))
    assert schedule.blocks == []
    assert any(issue.severity is Severity.INFO for issue in schedule.issues)


def test_planning_in_daylight_starts_at_dusk(planner):
    """Plans are built in the afternoon, and estimating from 'now' would
    put every block in the wrong part of the sky."""
    ephemeris = EphemerisService(PARIS)
    night = ephemeris.night_window()
    if night.astronomical_dusk is None:  # pragma: no cover - polar summer
        pytest.skip("no astronomical darkness tonight")

    noon = night.astronomical_dusk - timedelta(hours=6)
    plan = SessionPlan(name="Afternoon", blocks=[_block(POLAR, frames=5, exposure_s=60.0)])
    schedule = planner.schedule(plan, start=noon)

    assert schedule.starts_at >= night.astronomical_dusk


def test_a_plan_started_after_dusk_starts_when_asked(planner):
    ephemeris = EphemerisService(PARIS)
    night = ephemeris.night_window()
    if night.astronomical_dusk is None:  # pragma: no cover - polar summer
        pytest.skip("no astronomical darkness tonight")

    later = night.astronomical_dusk + timedelta(hours=1)
    plan = SessionPlan(name="Late", blocks=[_block(POLAR, frames=5, exposure_s=60.0)])
    assert planner.schedule(plan, start=later).starts_at == later


def test_plan_totals_add_up():
    plan = SessionPlan(
        name="Sum",
        blocks=[
            _block(M31, frames=10, exposure_s=60.0),
            _block(M31, frames=20, exposure_s=30.0),
        ],
    )
    assert plan.integration_s == pytest.approx(10 * 60 + 20 * 30)
    assert plan.duration_s == pytest.approx(sum(b.duration_s for b in plan.blocks))


def test_now_is_used_when_no_start_is_given(planner):
    """Sanity: the schedule never starts in the past."""
    plan = SessionPlan(name="Now", blocks=[_block(POLAR, frames=2, exposure_s=10.0)])
    schedule = planner.schedule(plan)
    assert schedule.starts_at >= datetime.now(UTC) - timedelta(seconds=5)
