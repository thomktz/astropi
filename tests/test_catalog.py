"""Catalogue loading, search ranking and ephemeris."""

from __future__ import annotations

import pytest

from astropi.core.geometry import RaDec
from astropi.core.site import ObservingSite
from astropi.services.catalog import CatalogService, TargetSource
from astropi.services.ephemeris import EphemerisService


@pytest.fixture(scope="module")
def catalog():
    site = ObservingSite(latitude_deg=48.8566, longitude_deg=2.3522)
    return CatalogService(EphemerisService(site))


def test_catalog_loads_all_three_sources(catalog):
    sources = {t.source for t in catalog.all(include_planets=False)}
    assert sources == {TargetSource.MESSIER, TargetSource.NGC, TargetSource.STAR}
    assert len(catalog) > 1000


def test_messier_coordinates_are_right(catalog):
    m31 = catalog.get("m31")
    assert m31 is not None
    assert m31.coord.ra_deg == pytest.approx(10.6847, abs=0.01)
    assert m31.coord.dec_deg == pytest.approx(41.269, abs=0.01)
    assert "Andromeda Galaxy" in m31.common_names


def test_search_prefers_the_object_over_its_constellation(catalog):
    results = catalog.search("andromeda", limit=5)
    assert results[0][0].display_name == "Andromeda Galaxy"


def test_search_matches_a_catalogue_designation(catalog):
    results = catalog.search("m42", limit=3)
    assert results[0][0].name.lower() == "m42"


def test_search_matches_a_proper_star_name(catalog):
    results = catalog.search("vega", limit=3)
    assert results[0][0].name.lower() == "vega"


def test_search_can_filter_to_what_is_up(catalog):
    results = catalog.search("", limit=50, min_altitude_deg=20.0)
    assert all(altitude >= 20.0 for _, altitude in results)


def test_planets_are_positioned_at_query_time(catalog):
    planets = {t.id for t in catalog.planets()}
    assert {"jupiter", "saturn", "mars"} <= planets


def test_star_array_for_the_simulator(catalog):
    array = catalog.as_star_array(max_magnitude=8.0)
    assert array.shape[1] == 3
    assert (array[:, 2] <= 8.0).all()
    assert ((array[:, 0] >= 0) & (array[:, 0] < 360)).all()


def test_circumpolar_depends_on_latitude():
    """M31 never sets from Paris, and barely rises from Sydney."""
    m31 = RaDec(10.6847, 41.2690)

    paris = EphemerisService(ObservingSite(48.8566, 2.3522))
    sydney = EphemerisService(ObservingSite(-33.87, 151.21))

    assert paris.visibility(m31).circumpolar is True
    assert sydney.visibility(m31).circumpolar is False
    assert sydney.visibility(m31).max_altitude_deg < paris.visibility(m31).max_altitude_deg


def test_never_rises_from_the_wrong_hemisphere():
    """The south celestial pole is permanently below a Paris horizon."""
    ephemeris = EphemerisService(ObservingSite(48.8566, 2.3522))
    assert ephemeris.visibility(RaDec(0.0, -85.0)).never_rises is True


def test_night_window_has_a_dark_period():
    window = EphemerisService(ObservingSite(48.8566, 2.3522)).night_window()
    assert window.astronomical_dusk is not None
    assert window.astronomical_dawn is not None
    assert 0.0 < window.dark_hours < 14.0
    assert 0.0 <= window.moon_illumination <= 1.0
