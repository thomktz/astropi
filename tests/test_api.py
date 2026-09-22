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


def test_socket_opens_with_current_state_not_just_history(client):
    """State the client cannot infer from events must be sent on connect.

    The replayed history is a ring buffer, and during guiding the samples
    push older state events out of it - so a dashboard opened mid-session
    would otherwise show guiding as stopped while the loop was running.
    """
    with client.websocket_connect("/ws") as socket:
        # Drain until the last of the opening messages. Reading a fixed
        # count would block whenever the replayed history happened to be
        # shorter than that count; the guard has to sit clear of the event
        # bus's 200-entry history, which is replayed first.
        topics: list[str] = []
        while "guiding.state" not in topics and len(topics) < 400:
            topics.append(socket.receive_json()["topic"])

    assert topics[0] == "hello"
    # Last, after everything replayed from the history, so a stale buffered
    # event cannot overwrite the live value.
    assert topics[-2:] == ["target.active", "guiding.state"]


def test_the_guide_view_can_be_kept_live_without_guiding(client):
    """The guide sensor can keep exposing while the loop is stopped.

    The moment you most need to see through it is *before* guiding runs -
    choosing a star, checking its focus, seeing cloud arrive - so the
    sub-display has a switch of its own.
    """
    assert client.get("/api/guiding").json()["state"] == "stopped"
    client.put("/api/guiding/settings", json={"preview_enabled": True, "preview_period_s": 0.0})

    deadline = time.time() + 20.0
    while time.time() < deadline:
        if client.get("/api/guiding/frame").json() is not None:
            break
        time.sleep(0.3)

    info = client.get("/api/guiding/frame").json()
    client.put("/api/guiding/settings", json={"preview_enabled": False})
    assert info is not None, "the idle guide loop produced no frame"
    assert client.get("/api/guiding/frame.png").status_code == 200


def test_guide_preview_produces_a_frame_and_candidates(client):
    """A single exposure without guiding, so a star can be picked first."""
    client.post("/api/mount/unpark")
    preview = client.post("/api/guiding/preview").json()
    assert preview["stars"] > 0

    info = client.get("/api/guiding/frame").json()
    assert info["width"] == 1280
    assert info["height"] == 960
    assert info["search_radius_px"] > 0
    assert len(info["candidates"]) > 0
    # Nothing is locked until something locks it.
    assert info["lock"] is None

    image = client.get("/api/guiding/frame.png")
    assert image.status_code == 200
    assert image.content[:8] == b"\x89PNG\x0d\x0a\x1a\x0a"
    # Never cached - the point of this image is that it is the newest one.
    assert "no-store" in image.headers["cache-control"]


def test_clicking_a_star_locks_onto_it(client):
    """The automatic pick is the brightest star, which is often not the one."""
    client.post("/api/mount/unpark")
    client.post("/api/guiding/preview")
    candidates = client.get("/api/guiding/frame").json()["candidates"]

    # Something other than the brightest, to prove the choice is honoured.
    wanted = sorted(candidates, key=lambda c: c["snr"], reverse=True)[min(3, len(candidates) - 1)]
    locked = client.post("/api/guiding/lock", json={"x": wanted["x"], "y": wanted["y"]}).json()

    assert locked["x"] == pytest.approx(wanted["x"], abs=0.01)
    assert locked["y"] == pytest.approx(wanted["y"], abs=0.01)
    assert client.get("/api/guiding/frame").json()["lock"]["x"] == pytest.approx(wanted["x"], abs=0.01)


def test_clicking_empty_sky_is_refused_with_a_useful_message(client):
    client.post("/api/mount/unpark")
    client.post("/api/guiding/preview")

    response = client.post("/api/guiding/lock", json={"x": 5.0, "y": 5.0, "radius_px": 10.0})
    assert response.status_code == 400
    assert "no star detected" in response.json()["detail"]


def test_a_freshly_started_rig_is_doing_nothing(tmp_path_factory):
    """Nothing that acts on the sky runs until it is asked to.

    Opening the dashboard must never find the mount tracking, the guider
    running or the cooler on because of something a previous session left
    behind - the rig starts stowed, every time, live view included.

    Its own app, not the module-level client, whose rig other tests have
    been driving.
    """
    settings = Settings(camera_width=600, camera_height=400, data_dir=tmp_path_factory.mktemp("d"))
    with TestClient(create_app(settings)) as fresh:
        mount = fresh.get("/api/mount").json()
        assert mount["state"] == "parked"
        assert mount["tracking"] is False

        guiding = fresh.get("/api/guiding").json()
        assert guiding["state"] == "stopped"
        assert guiding["calibrated"] is False

        camera = fresh.get("/api/camera").json()
        assert camera["state"] == "idle"
        assert camera["cooling"]["enabled"] is False

        # Neither sensor loops until it is asked to, the guide one included.
        assert fresh.get("/api/camera/preview").json()["enabled"] is False
        assert fresh.get("/api/guiding/settings").json()["preview_enabled"] is False

        assert fresh.get("/api/targets/active").json() is None
        assert fresh.get("/api/tasks/current").json() is None


def test_guiding_settings_are_readable_and_changeable(client):
    defaults = client.get("/api/guiding/settings").json()
    assert defaults["exposure_s"] > 0
    assert defaults["dec_mode"] == "auto"

    updated = client.put(
        "/api/guiding/settings", json={"exposure_s": 3.5, "gain": 180, "dec_mode": "north"}
    ).json()
    assert updated["exposure_s"] == pytest.approx(3.5)
    assert updated["gain"] == 180
    assert updated["dec_mode"] == "north"
    # A partial update leaves everything else alone.
    assert updated["ra_aggressiveness"] == defaults["ra_aggressiveness"]

    client.put("/api/guiding/settings", json=defaults | {"dec_mode": "auto"})


def test_nonsense_guiding_settings_are_rejected(client):
    assert client.put("/api/guiding/settings", json={"exposure_s": -1}).status_code == 422
    assert client.put("/api/guiding/settings", json={"dec_mode": "sideways"}).status_code == 422


def test_live_view_settings_round_trip(client):
    defaults = client.get("/api/camera/preview").json()
    assert defaults["enabled"] is False

    updated = client.put(
        "/api/camera/preview", json={"enabled": True, "exposure_s": 1.5, "binning": 4}
    ).json()
    assert updated["enabled"] is True
    assert updated["exposure_s"] == pytest.approx(1.5)
    assert updated["binning"] == 4
    # Untouched settings stay put.
    assert updated["gain"] == defaults["gain"]

    client.put("/api/camera/preview", json={"enabled": False, "exposure_s": defaults["exposure_s"]})


def test_one_live_frame_without_the_loop(client):
    """Refresh takes a frame, shows it, and leaves the loop alone."""
    assert client.get("/api/camera/preview").json()["enabled"] is False

    body = client.post("/api/camera/preview/frame").json()
    assert body["width"] > 0

    view = client.get("/api/camera/view").json()
    assert view["source"] == "preview"
    assert view["captured_at"] == pytest.approx(body["captured_at"])
    assert client.get("/api/camera/preview").json()["enabled"] is False


def test_the_main_display_stays_on_the_live_view(client):
    """A capture opens in its own overlay; the display keeps showing now.

    The old rule - whichever of the two is newer - left the main display
    frozen on a still picture after every deliberate frame, which is the
    opposite of what a live view is for.
    """
    client.put("/api/camera/preview", json={"enabled": True})
    deadline = time.time() + 20.0
    while time.time() < deadline and client.get("/api/camera/view").json() is None:
        time.sleep(0.3)

    client.post("/api/camera/expose", json={"duration_s": 1.0})
    try:
        assert client.get("/api/camera/view").json()["source"] == "preview"

        image = client.get("/api/camera/view.png")
        assert image.status_code == 200
        assert image.content[:8] == b"\x89PNG\x0d\x0a\x1a\x0a"
        assert "no-store" in image.headers["cache-control"]
    finally:
        # One rig, shared by the module: leave the loop as it was found.
        client.put("/api/camera/preview", json={"enabled": False})


def test_the_view_falls_back_to_a_stored_frame(client):
    """With the live view off, the display shows the last real capture."""
    client.put("/api/camera/preview", json={"enabled": False})
    try:
        client.post("/api/camera/expose", json={"duration_s": 1.0})
        view = client.get("/api/camera/view").json()
        assert view["source"] == "frame"
        assert view["frame_id"] is not None
    finally:
        client.put("/api/camera/preview", json={"enabled": True})


def test_controls_advertise_limits_and_writability(client):
    """The client holds no table of controls; it renders what it is told."""
    controls = {item["name"]: item for item in client.get("/api/camera/controls").json()}

    gain = controls["gain"]
    assert gain["writable"] is True
    assert (gain["minimum"], gain["maximum"]) == (0.0, 500.0)

    # Measurements belong in the list too, marked as what they are.
    assert controls["sensor_temp"]["writable"] is False
    assert controls["cooler_power"]["unit"] == "%"
    assert controls["dew_heater"]["kind"] == "boolean"


def test_setting_a_control_reports_the_value_back(client):
    body = client.put("/api/camera/controls/gain", json={"value": 260}).json()
    assert body["value"] == 260.0
    assert client.get("/api/camera").json()["gain"] == 260


def test_control_values_are_clamped_to_the_advertised_range(client):
    body = client.put("/api/camera/controls/usb_bandwidth", json={"value": 900}).json()
    assert body["value"] == 100.0


def test_read_only_controls_cannot_be_written(client):
    response = client.put("/api/camera/controls/cooler_power", json={"value": 50})
    assert response.status_code == 501


def test_unknown_controls_are_refused(client):
    assert client.put("/api/camera/controls/warp_core", json={"value": 1}).status_code == 501


def test_guide_camera_advertises_a_smaller_control_set(client):
    names = {
        item["name"]
        for item in client.get("/api/camera/controls", params={"role": "guide"}).json()
    }
    assert "gain" in names
    # No cooler on the Duo's guide chip, so no cooler controls either.
    assert not names & {"cooler_on", "target_temp", "sensor_temp", "dew_heater"}


def test_cooling_controls_and_the_cooling_route_agree(client):
    client.put("/api/camera/controls/target_temp", json={"value": -15})
    client.put("/api/camera/controls/cooler_on", json={"value": 1})

    cooling = client.get("/api/camera").json()["cooling"]
    assert cooling["enabled"] is True
    assert cooling["target_c"] == -15.0
