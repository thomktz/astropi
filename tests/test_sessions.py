"""Session plans through the API, and on disk."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings
from astropi.core.geometry import RaDec
from astropi.services.planning import PlanBlock, SessionPlan
from astropi.storage.sessions import SessionStore


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    settings = Settings(
        camera_width=1200,
        camera_height=900,
        simulator_time_scale=0.02,
        simulator_slew_rate_deg_per_s=400.0,
        simulator_solve_seconds=0.0,
        # Plans are written to disk; keep them out of the real data folder.
        data_dir=tmp_path_factory.mktemp("data"),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _plan() -> SessionPlan:
    return SessionPlan(
        name="A night",
        blocks=[
            PlanBlock(
                target_id="m31",
                target_name="Andromeda Galaxy",
                coord=RaDec(10.6847, 41.269),
                frames=20,
                exposure_s=120.0,
                gain=100,
            )
        ],
    )


def test_store_round_trips_a_plan(store):
    saved = store.save(_plan())
    loaded = store.load(saved.id)

    assert loaded is not None
    assert loaded.name == "A night"
    assert len(loaded.blocks) == 1
    assert loaded.blocks[0].target_id == "m31"
    assert loaded.blocks[0].frames == 20
    assert loaded.blocks[0].coord.ra_deg == pytest.approx(10.6847)


def test_store_writes_readable_json(store):
    """A plan should be editable in a text editor - that is why it is JSON."""
    saved = store.save(_plan())
    raw = json.loads((store._path(saved.id)).read_text())
    assert raw["name"] == "A night"
    assert raw["blocks"][0]["frames"] == 20


def test_store_survives_a_corrupt_file(store, tmp_path):
    """One hand-edited typo must not take the other plans down with it."""
    store.save(_plan())
    (store._root / "broken.json").write_text("{not json")

    plans = store.list()
    assert len(plans) == 1
    assert plans[0].name == "A night"


def test_store_delete(store):
    saved = store.save(_plan())
    assert store.delete(saved.id) is True
    assert store.load(saved.id) is None
    assert store.delete(saved.id) is False


def test_plan_ids_cannot_escape_the_folder(store):
    """Ids are generated here, but a crafted one must not write elsewhere."""
    path = store._path("../../etc/passwd")
    assert store._root in path.parents


# ----------------------------------------------------------------- the API


def test_preview_schedules_without_saving(client):
    body = client.post(
        "/api/sessions/preview",
        json={
            "name": "Draft",
            "blocks": [{"target_id": "m31", "frames": 30, "exposure_s": 120}],
        },
    ).json()

    assert body["integration_s"] == pytest.approx(3600.0)
    assert body["duration_s"] > body["integration_s"], "wall clock includes overheads"
    assert len(body["blocks"]) == 1
    assert body["blocks"][0]["target_name"] == "Andromeda Galaxy"
    assert body["blocks"][0]["starts_at"] < body["blocks"][0]["ends_at"]
    # Nothing was written.
    assert all(plan["name"] != "Draft" for plan in client.get("/api/sessions").json())


def test_create_list_update_and_delete(client):
    created = client.post(
        "/api/sessions",
        json={"name": "Autumn", "blocks": [{"target_id": "m31", "frames": 10, "exposure_s": 60}]},
    ).json()
    plan_id = created["id"]

    assert any(plan["id"] == plan_id for plan in client.get("/api/sessions").json())
    assert client.get(f"/api/sessions/{plan_id}").json()["name"] == "Autumn"

    updated = client.put(
        f"/api/sessions/{plan_id}",
        json={
            "name": "Autumn, revised",
            "blocks": [
                {"target_id": "m31", "frames": 10, "exposure_s": 60},
                {"target_id": "m13", "frames": 5, "exposure_s": 90},
            ],
        },
    ).json()
    assert updated["name"] == "Autumn, revised"
    assert len(updated["blocks"]) == 2

    assert client.delete(f"/api/sessions/{plan_id}").json()["deleted"] is True
    assert client.get(f"/api/sessions/{plan_id}").status_code == 404


def test_a_block_can_be_a_bare_coordinate(client):
    body = client.post(
        "/api/sessions/preview",
        json={
            "name": "Comet",
            "blocks": [
                {
                    "target_name": "Comet somebody",
                    "ra_deg": 120.0,
                    "dec_deg": 20.0,
                    "frames": 10,
                    "exposure_s": 30,
                }
            ],
        },
    ).json()
    assert body["blocks"][0]["target_name"] == "Comet somebody"


def test_a_block_needs_a_target(client):
    response = client.post(
        "/api/sessions/preview",
        json={"name": "Bad", "blocks": [{"frames": 10, "exposure_s": 30}]},
    )
    assert response.status_code == 400
    assert "target_id" in response.json()["detail"]


def test_an_unknown_target_is_a_404(client):
    response = client.post(
        "/api/sessions/preview",
        json={"name": "Bad", "blocks": [{"target_id": "m999", "frames": 10, "exposure_s": 30}]},
    )
    assert response.status_code == 404


def test_an_empty_plan_cannot_be_run(client):
    created = client.post("/api/sessions", json={"name": "Empty", "blocks": []}).json()
    assert client.post(f"/api/sessions/{created['id']}/run").status_code == 400


def test_running_a_plan_centres_and_captures_each_block(client):
    """The headline: a plan walks its blocks, centring then shooting."""
    import time

    created = client.post(
        "/api/sessions",
        json={
            "name": "Two blocks",
            "blocks": [
                {"target_id": "m31", "frames": 2, "exposure_s": 4, "dither_every": 0},
                {"target_id": "m81", "frames": 1, "exposure_s": 4, "dither_every": 0},
            ],
        },
    ).json()

    client.post("/api/mount/unpark")
    task = client.post(f"/api/sessions/{created['id']}/run").json()
    assert task["kind"] == "session"

    deadline = time.time() + 120
    while time.time() < deadline:
        current = client.get(f"/api/tasks/{task['id']}").json()
        if current["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    else:  # pragma: no cover - only on a stall
        raise AssertionError("session did not finish")

    assert current["state"] == "succeeded", current["error"]
    joined = " ".join(current["messages"])
    # Sub-tasks report into the session, prefixed with their block number,
    # rather than publishing as tasks of their own.
    assert "[1/2]" in joined and "[2/2]" in joined
    assert "Centred" in joined
    assert "Captured 3 frames across 2 block(s)" in joined
