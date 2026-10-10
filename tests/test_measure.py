"""Tests for app.services.measure (Phase 1)."""

from __future__ import annotations

import pytest
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString, Point, Polygon

from app.services.measure import measure


def test_polygon_area_analytical():
    # 100 m x 50 m rectangle -> 5000 m2.
    geom = Polygon([(0, 0), (100, 0), (100, 50), (0, 50)])
    out = measure(geom, "area")
    assert out == {"type": "area", "value": 5000.0, "unit": "m2"}


def test_line_length_analytical():
    # 3-4-5 triangle scaled: (0,0)-(300,0) -> 300 m.
    geom = LineString([(0, 0), (300, 0)])
    out = measure(geom, "length")
    assert out == {"type": "length", "value": 300.0, "unit": "m"}
    # Diagonal check: 30-40-50.
    diag = LineString([(0, 0), (30, 40)])
    assert measure(diag, "length")["value"] == pytest.approx(50.0)


def test_units_and_structure():
    area = measure(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), "area")
    assert set(area.keys()) == {"type", "value", "unit"}
    assert area["type"] == "area" and area["unit"] == "m2"
    length = measure(LineString([(0, 0), (10, 0)]), "length")
    assert set(length.keys()) == {"type", "value", "unit"}
    assert length["type"] == "length" and length["unit"] == "m"


def test_geographic_must_be_reprojected():
    # 0.001 x 0.001 degree square at the equator. Naive degree-area is 1e-6
    # "square degrees" (meaningless). After reprojection to UTM 31N the area
    # is ~12.3 km2. This test proves callers must reproject first.
    geo = Polygon([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)])
    naive = geo.area
    assert naive == pytest.approx(1e-6)
    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_epsg(32631), always_xy=True
    )
    from shapely.ops import transform as shp_transform

    projected = shp_transform(transformer.transform, geo)
    out = measure(projected, "area")
    assert out["value"] > 10_000  # ~12,300 m2, not 1e-6
    assert out["value"] == pytest.approx(projected.area)


def test_projected_area_matches_geodesic_reference():
    # Independent reference: pyproj.Geod geodesic area for a small square at
    # the equator (0.01 deg side, ~1113 m). UTM projection distortion at this
    # scale is small; allow 1% tolerance with justification: transverse
    # Mercator scale error grows with distance from central meridian but stays
    # well under 0.1% within a few degrees for such a small polygon; 1% is a
    # conservative bound that still catches degree-vs-metre mistakes (which
    # would be off by ~1e10x).
    geod = Geod(ellps="WGS84")
    coords = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]
    ring = Polygon(coords)
    geo_area, _ = geod.geometry_area_perimeter(ring)
    geo_area = abs(geo_area)
    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_epsg(32631), always_xy=True
    )
    from shapely.ops import transform as shp_transform

    projected = shp_transform(transformer.transform, ring)
    out = measure(projected, "area")
    assert out["value"] == pytest.approx(geo_area, rel=0.01)


def test_empty_geometry_returns_none():
    assert measure(Polygon(), "area") is None
    assert measure(LineString(), "length") is None
    assert measure(None, "area") is None


def test_non_applicable_kind_returns_none():
    geom = Point(5, 5)
    assert measure(geom, "none") is None


def test_unsupported_status_kind_returns_none():
    geom = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert measure(geom, "unsupported") is None


def test_unknown_kind_raises():
    geom = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    with pytest.raises(ValueError, match="unsupported measurement kind"):
        measure(geom, "volume")


def test_result_types_are_builtin_float():
    out = measure(Polygon([(0, 0), (100, 0), (100, 50), (0, 50)]), "area")
    assert type(out["value"]) is float
    assert isinstance(out["value"], float) and not hasattr(out["value"], "dtype")
    out2 = measure(LineString([(0, 0), (3, 4)]), "length")
    assert type(out2["value"]) is float


def test_non_finite_rejected():
    class FakeGeom:
        is_empty = False

        def __init__(self, v):
            self._v = v

        @property
        def area(self):
            return self._v

        @property
        def length(self):
            return self._v

    with pytest.raises(ValueError, match="non-finite"):
        measure(FakeGeom(float("inf")), "area")
    with pytest.raises(ValueError, match="non-finite"):
        measure(FakeGeom(float("nan")), "length")
