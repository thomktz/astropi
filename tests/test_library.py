"""Browsing the frame archive and its cached JPEG previews."""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits
from fastapi.testclient import TestClient

from astropi.api.app import create_app
from astropi.config import Settings
from astropi.core.errors import AstropiError


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
        assert (frames / ".previews" / "2026-10-08_M31" / "LIGHT" / "M31_LIGHT_0001.thumb.colour.jpg").exists()
        assert [d["name"] for d in client.get("/api/library").json()["dirs"]] == ["2026-10-08_M31"]

        assert client.get("/api/library", params={"path": "../"}).status_code == 400


def test_full_quality_is_the_frame_at_its_own_size_in_colour(tmp_path):
    from PIL import Image

    frames = tmp_path / "frames"
    light = frames / "2026-10-08_M31" / "LIGHT"
    light.mkdir(parents=True)
    data = np.random.default_rng(2).integers(800, 1200, size=(300, 400), dtype=np.uint16)
    hdu = fits.PrimaryHDU(data)
    hdu.header["BAYERPAT"] = "RGGB"
    hdu.writeto(light / "M31_LIGHT_0001.fits")

    with TestClient(create_app(Settings(data_dir=tmp_path, frames_dir=frames))) as client:
        jpeg = client.get(
            "/api/library/preview.jpg",
            params={"path": "2026-10-08_M31/LIGHT/M31_LIGHT_0001.fits", "size": "full"},
        )
        assert jpeg.status_code == 200
    image = Image.open(frames / ".previews" / "2026-10-08_M31" / "LIGHT" / "M31_LIGHT_0001.full.colour.jpg")
    assert image.size == (400, 300) and image.mode == "RGB"


def test_frames_and_folders_are_deleted_with_their_previews(tmp_path):
    frames = tmp_path / "frames"
    light = frames / "2026-10-08_M31" / "LIGHT"
    light.mkdir(parents=True)
    data = np.full((40, 60), 1000, dtype=np.uint16)
    for n in (1, 2):
        fits.PrimaryHDU(data).writeto(light / f"M31_LIGHT_000{n}.fits")
    first = "2026-10-08_M31/LIGHT/M31_LIGHT_0001.fits"

    with TestClient(create_app(Settings(data_dir=tmp_path, frames_dir=frames))) as client:
        client.get("/api/library/preview.jpg", params={"path": first})
        summary = client.get("/api/library/summary", params={"path": "2026-10-08_M31"}).json()
        assert (summary["kind"], summary["files"], summary["frames"]) == ("folder", 2, 2)

        assert client.delete("/api/library", params={"path": first}).json()["kind"] == "frame"
        assert not (light / "M31_LIGHT_0001.fits").exists()
        assert not list((frames / ".previews").rglob("M31_LIGHT_0001.*"))

        # Nothing outside the archive, and not the archive itself.
        assert client.delete("/api/library", params={"path": "../frames"}).status_code == 400
        assert client.delete("/api/library", params={"path": ""}).status_code == 400

        assert client.delete("/api/library", params={"path": "2026-10-08_M31"}).json()["files"] == 1
        assert not (frames / "2026-10-08_M31").exists()
        assert not (frames / ".previews" / "2026-10-08_M31").exists()


def test_the_folder_a_sequence_is_writing_cannot_be_deleted(tmp_path):
    from astropi.storage.browser import FrameBrowser

    light = tmp_path / "2026-10-08_M31" / "LIGHT"
    light.mkdir(parents=True)
    fits.PrimaryHDU(np.zeros((4, 8), dtype=np.uint16)).writeto(light / "M31_LIGHT_0001.fits")
    browser = FrameBrowser(tmp_path, writing=lambda: light)
    for path in ("2026-10-08_M31", "2026-10-08_M31/LIGHT", "2026-10-08_M31/LIGHT/M31_LIGHT_0001.fits"):
        with pytest.raises(AstropiError, match="running sequence"):
            browser.delete(path)
    assert (light / "M31_LIGHT_0001.fits").exists()
