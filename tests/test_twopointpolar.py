"""Two-frame polar alignment: the fit, the drawing, and the HTTP flow."""

from __future__ import annotations

import random
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings
from astropi.core.errors import AstropiError
from astropi.core.geometry import RaDec, normalize_deg
from astropi.core.pointing import misaligned_pole, true_pole, vector_to_radec
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import local_sidereal_time_deg
from astropi.devices.backends.simulator.sky import OpticalTrain, project
from astropi.services.platesolve import SolveResult, _astap_result, _simulated_cd
from astropi.services.twopointpolar import PolarFrame, correction, fit_two_frames, live_error

SITE = ObservingSite(latitude_deg=47.6, longitude_deg=-122.3)
ALT_ERROR, AZ_ERROR = 0.7, -1.3
HA_OFFSET, DEC_OFFSET = 3.2, -0.4


def shot(lst: float, ha: float, dec: float, *, noise_arcsec: float = 0.0, seed: int = 1) -> PolarFrame:
    """A frame from a mount with a known misalignment and unsynced axes."""
    rng = random.Random(seed)
    axis = misaligned_pole(SITE.latitude_deg, ALT_ERROR, AZ_ERROR)
    sky = vector_to_radec(axis.pointing(ha + HA_OFFSET, dec + DEC_OFFSET), lst, true_pole(SITE.latitude_deg))
    jitter = noise_arcsec / 3600.0
    sky = RaDec(sky.ra_deg + rng.gauss(0, jitter), sky.dec_deg + rng.gauss(0, jitter))
    return PolarFrame(solved=sky, mount=RaDec(normalize_deg(lst - ha), dec), lst_deg=lst)


def test_an_ra_move_recovers_the_axis_and_the_offsets():
    fit = fit_two_frames(shot(100.0, -40.0, 50.0), shot(100.5, -10.0, 50.0), SITE)
    assert fit.altitude_error_deg == pytest.approx(ALT_ERROR, abs=1e-4)
    assert fit.azimuth_error_deg == pytest.approx(AZ_ERROR, abs=1e-4)
    assert fit.ha_offset_deg == pytest.approx(HA_OFFSET, abs=1e-4)
    assert fit.dec_offset_deg == pytest.approx(DEC_OFFSET, abs=1e-4)
    assert fit.rotation_deg == pytest.approx(30.0)
    assert fit.warnings == []


def test_a_move_with_declination_still_fits_but_says_so():
    fit = fit_two_frames(shot(100.0, -40.0, 50.0), shot(100.5, -5.0, 55.0), SITE)
    assert fit.altitude_error_deg == pytest.approx(ALT_ERROR, abs=1e-4)
    assert any("Declination moved" in note for note in fit.warnings)


def test_solve_noise_costs_a_short_move_more():
    long = fit_two_frames(shot(100.0, -40.0, 50.0), shot(100.5, 0.0, 50.0), SITE)
    short = fit_two_frames(shot(100.0, -40.0, 50.0), shot(100.5, -28.0, 50.0), SITE)
    assert short.uncertainty_arcmin > long.uncertainty_arcmin
    assert any("only 12" in note for note in short.warnings)


def test_too_small_a_move_is_refused():
    with pytest.raises(AstropiError, match="turned only"):
        fit_two_frames(shot(100.0, -40.0, 50.0), shot(100.1, -38.0, 50.0), SITE)


def test_live_frame_after_the_knobs_turn_reads_the_new_axis():
    fit = fit_two_frames(shot(100.0, -40.0, 50.0, noise_arcsec=3), shot(100.5, -10.0, 50.0, seed=2), SITE)
    error, knobs = live_error(fit, shot(101.0, -10.0, 50.0), SITE)
    assert error.altitude_error_arcmin == pytest.approx(ALT_ERROR * 60, abs=2)
    assert knobs[1] == pytest.approx(AZ_ERROR, abs=0.05)

    plan = correction(fit, shot(101.0, -10.0, 50.0), knobs, SITE)
    # The aligned position is where a perfectly aligned axis would put the
    # same mechanical angles - i.e. where the field has to go.
    aligned_axis = misaligned_pole(SITE.latitude_deg, 0.0, 0.0)
    expected = vector_to_radec(
        aligned_axis.pointing(-10.0 + HA_OFFSET, 50.0 + DEC_OFFSET), 101.0, true_pole(SITE.latitude_deg)
    )
    assert plan.aligned.ra_deg == pytest.approx(expected.ra_deg, abs=0.05)
    assert plan.aligned.dec_deg == pytest.approx(expected.dec_deg, abs=0.05)


def test_simulated_wcs_matches_the_simulated_projection():
    optics = OpticalTrain(
        width=1800, height=1400, pixel_size_um=3.76, focal_length_mm=400.0, rotation_deg=27.0
    )
    center = RaDec(120.0, 50.0)
    star = RaDec(120.6, 50.4)
    x, y = project(np.array([[star.ra_deg, star.dec_deg]]), center, optics)
    result = SolveResult(
        center=center,
        pixel_scale_arcsec=optics.pixel_scale_arcsec,
        rotation_deg=27.0,
        flipped=False,
        stars_detected=50,
        solve_time_s=0.1,
        solver="test",
        reference_pixel=(900.0, 700.0),
        cd=_simulated_cd(optics.pixel_scale_arcsec, 27.0),
    )
    px, py = result.pixel_of(star)
    assert px == pytest.approx(float(x[0]), abs=0.01)
    assert py == pytest.approx(float(y[0]), abs=0.01)


def test_astap_fields_scale_back_to_the_full_frame():
    fields = {
        "PLTSOLVD": "T",
        "CRVAL1": "83.8",
        "CRVAL2": "-5.39",
        "CRPIX1": "781.5",
        "CRPIX2": "522.5",
        "CD1_1": "-0.002",
        "CD1_2": "0.0",
        "CD2_1": "0.0",
        "CD2_2": "0.002",
        "CROTA2": "0.0",
    }
    result = _astap_result(fields, factor=4, elapsed_s=1.0)
    # Binned pixel 781.5 (1-based) is the 0-based full-frame pixel 3123.5.
    assert result.reference_pixel == pytest.approx((3123.5, 2087.5))
    assert result.pixel_scale_arcsec == pytest.approx(1.8)
    assert result.flipped is False


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    settings = Settings(
        # Its own state file: the sky errors set below must not land in
        # the rig's real one.
        data_dir=tmp_path_factory.mktemp("data"),
        camera_width=1800,
        camera_height=1400,
        simulator_time_scale=0.02,
        simulator_slew_rate_deg_per_s=400.0,
        simulator_solve_seconds=0.0,
        latitude_deg=48.8566,
        longitude_deg=2.3522,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def wait_for_slew(client: TestClient, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if client.get("/api/mount").json()["state"] != "slewing":
            return
        time.sleep(0.05)
    raise AssertionError("the slew did not finish")


def test_two_frames_over_http_measure_the_simulated_error(client):
    client.delete("/api/polar")
    client.post("/api/mount/unpark")
    client.put("/api/devices/simulator/sky", json={"enabled": True, "seeing_arcsec": 0})
    # Somewhere up in the east, with stars and room to move west.
    lst = local_sidereal_time_deg(2.3522)
    client.post("/api/mount/slew", json={"ra_deg": normalize_deg(lst + 40.0), "dec_deg": 40.0})
    wait_for_slew(client)
    # The simulated mount carries the polar error itself, as a real one would.
    true_alt, true_az = client.app.state.observatory.mount().polar_error

    first = client.post("/api/polar/capture", json={"exposure_s": 4.0, "binning": 1}).json()
    assert first["solve_error"] is None, first
    assert client.post("/api/polar/accept", json={"shot_id": first["id"]}).json()["step"] == "second"

    client.post("/api/mount/nudge", json={"direction": "west", "degrees": 30.0})
    second = client.post("/api/polar/capture", json={"exposure_s": 4.0, "binning": 1}).json()
    assert abs(second["moved_deg"]) == pytest.approx(30.0, abs=1.0)
    state = client.post("/api/polar/accept", json={"shot_id": second["id"]}).json()
    assert state["step"] == "live"
    assert state["fit"]["altitude_error_arcmin"] == pytest.approx(true_alt * 60, abs=3.0)
    assert state["fit"]["azimuth_error_arcmin"] == pytest.approx(true_az * 60, abs=3.0)

    live = client.post("/api/polar/live", json={"exposure_s": 4.0, "binning": 1}).json()
    assert live["error"]["total_error_arcmin"] > 0
    overlay = live["overlay"]
    assert overlay["start"] == [0.5, 0.5]
    assert overlay["aligned"] != overlay["start"]
