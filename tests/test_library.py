"""Browsing the frame archive and its cached JPEG previews."""

from __future__ import annotations

import numpy as np
from astropy.io import fits
from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings


def test_browse_and_preview(tmp_path):
    frames = tmp_path / "frames"
    light = frames / "2026-10-08_M31" / "LIGHT"
    light.mkdir(parents=True)
    data = np.random.default_rng(1).integers(800, 1200, size=(400, 600), dtype=np.uint16)
    fits.PrimaryHDU(data).writeto(light / "M31_LIGHT_0001.fits")

    with TestClient(create_app(Settings(data_dir=tmp_path, frames_dir=frames))) as client:
        root = client.get("/api/library").json()
        assert [d["name"] for d in root["dirs"]] == ["2026-10-08_M31"]

        folder = client.get("/api/library", params={"path": "2026-10-08_M31/LIGHT"}).json()
        assert [f["name"] for f in folder["frames"]] == ["M31_LIGHT_0001.fits"]

        jpeg = client.get("/api/library/preview.jpg", params={"path": folder["frames"][0]["path"]})
        assert jpeg.status_code == 200 and jpeg.content[:2] == b"\xff\xd8"
        # Cached beside the frames, and hidden from the listing.
        assert (frames / ".previews" / "2026-10-08_M31" / "LIGHT" / "M31_LIGHT_0001.thumb.jpg").exists()
        assert [d["name"] for d in client.get("/api/library").json()["dirs"]] == ["2026-10-08_M31"]

        assert client.get("/api/library", params={"path": "../"}).status_code == 400
