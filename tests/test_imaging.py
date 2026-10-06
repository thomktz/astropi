"""Imaging sessions, the zenith slew and the file names, on the simulated rig."""

from __future__ import annotations

import datetime as dt
import time

from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings
from astropi.storage.naming import ImageType, format_exposure, frame_stem, target_slug


def wait_for_task(client: TestClient, task_id: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] in ("succeeded", "failed", "cancelled"):
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish within {timeout_s}s")


def test_names():
    assert format_exposure(300.0) == "300s"
    assert format_exposure(0.85) == "0.85s"
    assert target_slug("Andromeda Galaxy (M31)") == "Andromeda-Galaxy-M31"
    assert target_slug("a/b") == "a-b"
    when = dt.datetime(2026, 10, 6, 1, 30, 12).astimezone()
    common = dict(target="M 31", exposure_s=300.0, gain=100, offset=50, temp_c=-10.0, started=when)
    assert (
        frame_stem(ImageType.LIGHT, filter_name="L-eNhance / dual-band", **common)
        == "M-31_LIGHT_LeNhance_300s_G100_O50_-10C_20261006-013012"
    )
    # Darks match whatever filter the lights used.
    assert frame_stem(ImageType.DARK, filter_name="L-eXtreme", **common).startswith("M-31_DARK_300s_")


def test_session_zenith_and_frames(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        camera_width=600,
        camera_height=400,
        simulator_time_scale=0.02,
        simulator_slew_rate_deg_per_s=400.0,
        simulator_solve_seconds=0.0,
        latitude_deg=48.8566,
        longitude_deg=2.3522,
    )
    with TestClient(create_app(settings)) as client:
        task = client.post("/api/tasks/zenith").json()
        assert wait_for_task(client, task["id"])["state"] == "succeeded"
        mount = client.get("/api/mount").json()
        assert mount["altitude_deg"] > 85 and not mount["tracking"]

        session = client.post("/api/imaging", json={"target_id": "m31"}).json()
        # Darks follow the lights until edited.
        session = client.patch(
            f"/api/imaging/{session['id']}",
            json={"groups": {"light": {"exposure_s": 2.0, "gain": 100, "offset": 50}, "dark": {"count": 2}}},
        ).json()
        dark = session["groups"]["dark"]
        assert (dark["exposure_s"], dark["gain"], dark["offset"], dark["linked"]) == (2.0, 100, 50, True)

        task = client.post(f"/api/imaging/{session['id']}/groups/dark/start", json={}).json()
        assert wait_for_task(client, task["id"])["state"] == "succeeded"
        files = sorted((tmp_path / "frames").glob("*_Andromeda-Galaxy/DARK/*.fits"))
        assert len(files) == 2
        assert files[0].name.startswith("Andromeda-Galaxy_DARK_2s_G100_O50_")
        assert client.get(f"/api/imaging/{session['id']}").json()["groups"]["dark"]["captured"] == 2
