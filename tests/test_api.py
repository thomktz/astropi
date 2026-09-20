"""End-to-end tests through HTTP, against the simulated rig."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings


@pytest.fixture(scope="module")
def client():
    settings = Settings(
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


def wait_for_task(client: TestClient, task_id: str, timeout_s: float = 60.0) -> dict:
    """Poll a task to completion, as the dashboard does over the socket."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] in ("succeeded", "failed", "cancelled"):
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish within {timeout_s}s")


@pytest.fixture(autouse=True)
def idle_rig(client):
    """Leave no task running behind, whatever a test does.

    The engine allows one task at a time, so a test that leaves one running
    turns every later submission into a 409 and the failure surfaces far
    from its cause.
    """
    yield
    current = client.get("/api/tasks/current").json()
    if current:
        client.delete(f"/api/tasks/{current['id']}")


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_system_describes_the_rig(client):
    body = client.get("/api/system").json()
    assert body["backend"] == "simulator"
    assert body["catalog_size"] > 1000
    assert set(body["devices"]) >= {"mount", "camera", "guide_camera", "focuser"}
    # 3.76 um at 400 mm is about 1.94 arcsec per pixel.
    assert body["optics"]["pixel_scale_arcsec"] == pytest.approx(1.94, abs=0.01)


def test_devices_are_connected(client):
    devices = client.get("/api/devices").json()
    assert all(d["connection"] == "connected" for d in devices.values())
    assert "pulse_guide" in devices["mount"]["capabilities"]
    assert "cooling" in devices["camera"]["capabilities"]
    # The guide sensor has no cooler, and must not claim one.
    assert "cooling" not in devices["guide_camera"]["capabilities"]


def test_target_search_ranks_the_galaxy_above_its_constellation(client):
    results = client.get("/api/targets/search", params={"q": "andromeda"}).json()
    assert results[0]["display_name"] == "Andromeda Galaxy"
    assert results[0]["coord"]["ra_hms"].startswith("00h42m")


def test_target_visibility_curve(client):
    body = client.get("/api/targets/m31/visibility").json()
    assert len(body["curve"]) > 200
    assert -90 <= body["max_altitude_deg"] <= 90
    # M31 is circumpolar from Paris: declination 41.3 against latitude 48.9.
    assert body["circumpolar"] is True


def test_night_window(client):
    body = client.get("/api/night").json()
    assert 0.0 <= body["moon_illumination"] <= 1.0
    assert body["dark_hours"] >= 0.0


def test_site_can_be_moved_and_changes_the_sky(client):
    original = client.get("/api/site").json()
    try:
        response = client.put(
            "/api/site",
            json={"latitude_deg": -33.87, "longitude_deg": 151.21, "name": "Sydney"},
        )
        assert response.json()["hemisphere"] == "south"
        # M31 is never far up from Sydney; it is circumpolar from Paris.
        assert client.get("/api/targets/m31/visibility").json()["circumpolar"] is False
    finally:
        client.put("/api/site", json=original)


def test_coordinates_accept_sexagesimal(client):
    response = client.post(
        "/api/targets/visibility", json={"ra": "00h42m44s", "dec": "+41d16m09s"}
    )
    assert response.status_code == 200
    assert response.json()["circumpolar"] is True


def test_mount_slew_park_and_tracking(client):
    client.post("/api/mount/unpark")
    response = client.post("/api/mount/slew", json={"ra_deg": 10.6847, "dec_deg": 41.269})
    assert response.status_code == 200

    body = client.post("/api/mount/tracking", json={"enabled": False}).json()
    assert body["tracking"] is False

    parked = client.post("/api/mount/park").json()
    assert parked["state"] == "parked"


def test_slew_below_horizon_is_rejected_with_a_clear_error(client):
    client.post("/api/mount/unpark")
    response = client.post("/api/mount/slew", json={"ra_deg": 0.0, "dec_deg": -89.0})
    assert response.status_code == 422
    assert "horizon" in response.json()["detail"]


def test_expose_and_fetch_a_preview(client):
    frame = client.post("/api/camera/expose", json={"duration_s": 2.0}).json()
    assert frame["width"] == 1800
    # Simulator ground truth must never leak out of the API.
    assert not any(key.startswith("sim_") for key in frame["metadata"])

    preview = client.get(f"/api/camera/frames/{frame['id']}/preview.png")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_cooling_can_be_enabled(client):
    body = client.post("/api/camera/cooling", json={"enabled": True, "target_c": -10.0}).json()
    assert body["enabled"] is True
    assert body["target_c"] == -10.0


def test_guide_camera_has_no_cooler(client):
    response = client.post(
        "/api/camera/cooling", params={"role": "guide"}, json={"enabled": True}
    )
    # 501: the request is valid, the hardware simply cannot do it.
    assert response.status_code == 501


def test_goto_task_centres_the_target(client):
    """The headline loop: slew, solve, sync, repeat until centred."""
    client.post("/api/mount/unpark")
    submitted = client.post(
        "/api/tasks/goto",
        json={"target_id": "m31", "tolerance_arcmin": 2.0, "exposure_s": 4.0},
    ).json()

    task = wait_for_task(client, submitted["id"])
    assert task["state"] == "succeeded", task["error"]
    assert task["detail"]["error_arcmin"] < 2.0

    joined = " ".join(task["messages"])
    assert "Centred" in joined


def test_a_second_task_is_refused_while_one_runs(client):
    """The rig is one set of hardware; two tasks would fight over it."""
    client.post("/api/mount/unpark")
    # A long capture, so the task is reliably still running on the next line.
    first = client.post(
        "/api/tasks/capture", json={"count": 200, "exposure_s": 5.0, "dither_every": 0}
    ).json()
    try:
        second = client.post("/api/tasks/goto", json={"target_id": "m31", "exposure_s": 4.0})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
    finally:
        client.delete(f"/api/tasks/{first['id']}")
        wait_for_task(client, first["id"])


def test_unknown_target_is_a_404(client):
    assert client.post("/api/tasks/goto", json={"target_id": "m999"}).status_code == 404


def test_goto_without_a_target_or_coordinate_is_a_400(client):
    assert client.post("/api/tasks/goto", json={}).status_code == 400


def test_polar_alignment_task_reports_an_error_and_instructions(client):
    client.post("/api/mount/unpark")
    submitted = client.post(
        "/api/tasks/polar-align", json={"points": 3, "separation_deg": 25.0, "exposure_s": 4.0}
    ).json()

    task = wait_for_task(client, submitted["id"])
    assert task["state"] == "succeeded", task["error"]

    detail = task["detail"]
    # The runtime's default simulated error is 0.35 deg altitude, -0.22 azimuth.
    assert detail["altitude_error_arcmin"] == pytest.approx(21.0, abs=3.0)
    assert detail["azimuth_error_arcmin"] == pytest.approx(-13.2, abs=3.0)
    assert any("Altitude" in line for line in detail["instructions"])


def test_autofocus_finds_the_best_position(client):
    submitted = client.post(
        "/api/tasks/autofocus", json={"steps": 9, "step_size": 400, "exposure_s": 3.0}
    ).json()

    task = wait_for_task(client, submitted["id"], timeout_s=120)
    assert task["state"] == "succeeded", task["error"]
    # The simulated focuser's true best position is 31400.
    joined = " ".join(task["messages"])
    assert "Focused at" in joined


def test_capture_sequence_stores_frames(client):
    submitted = client.post(
        "/api/tasks/capture", json={"count": 3, "exposure_s": 2.0, "dither_every": 0}
    ).json()

    task = wait_for_task(client, submitted["id"])
    assert task["state"] == "succeeded", task["error"]
    assert len(client.get("/api/camera/frames").json()) >= 3


def test_websocket_replays_recent_events(client):
    with client.websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        assert hello["topic"] == "hello"
        assert hello["payload"]["backend"] == "simulator"


def test_centring_frames_reach_the_frame_store(client):
    """The frames a GoTo solves are worth keeping.

    In an image-first dashboard they are the most useful thing on screen -
    they show the rig converging on the target - so dropping them left the
    viewer empty during the one operation worth watching.
    """
    before = len(client.get("/api/camera/frames").json())
    client.post("/api/mount/unpark")

    submitted = client.post(
        "/api/tasks/goto", json={"target_id": "m31", "tolerance_arcmin": 2.0, "exposure_s": 4.0}
    ).json()
    task = wait_for_task(client, submitted["id"])
    assert task["state"] == "succeeded", task["error"]

    frames = client.get("/api/camera/frames").json()
    assert len(frames) > before
    # And still no simulator ground truth on the way out.
    assert not any(key.startswith("sim_") for key in frames[0]["metadata"])


def test_active_target_is_named_by_a_goto_and_cleared_by_parking(client):
    """The named target is session state, not something a mount can report.

    A mount driver returns a coordinate and has no idea it is called M31,
    and the Mount protocol has to stay that way to work with any hardware -
    so the observatory remembers the name instead.
    """
    client.post("/api/targets/active/clear")
    assert client.get("/api/targets/active").json() is None

    client.post("/api/mount/unpark")
    submitted = client.post(
        "/api/tasks/goto", json={"target_id": "m31", "tolerance_arcmin": 2.0, "exposure_s": 4.0}
    ).json()
    wait_for_task(client, submitted["id"])

    active = client.get("/api/targets/active").json()
    assert active["id"] == "m31"
    assert active["display_name"] == "Andromeda Galaxy"
    assert active["ra_hms"].startswith("00h42m")
    assert -90 <= active["altitude_deg"] <= 90

    client.post("/api/mount/park")
    assert client.get("/api/targets/active").json() is None


def test_goto_by_coordinate_names_the_target_without_a_catalogue_entry(client):
    client.post("/api/targets/active/clear")
    client.post("/api/mount/unpark")
    submitted = client.post(
        "/api/tasks/goto",
        json={"coord": {"ra_deg": 83.822, "dec_deg": -5.391}, "max_iterations": 1, "exposure_s": 4.0},
    ).json()
    wait_for_task(client, submitted["id"])

    active = client.get("/api/targets/active").json()
    assert active is not None
    assert active["source"] == "custom"
    assert active["ra_hms"].startswith("05h35m")


def test_mount_status_reports_hour_angle(client):
    """Hour angle is what says when a meridian flip is due."""
    client.post("/api/mount/unpark")
    client.post("/api/mount/slew", json={"ra_deg": 10.6847, "dec_deg": 41.269})

    hour_angle = client.get("/api/mount").json()["hour_angle_deg"]
    assert hour_angle is not None
    # Wrapped to a half-turn either side: negative east, positive west.
    assert -180.0 <= hour_angle < 180.0
