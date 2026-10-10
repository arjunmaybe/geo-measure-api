"""Tests for app.services.crs (Phase 1).

Covers prompt §6 test_crs.py: northern/southern UTM, longitudes
-180/+180/6/12, projected-metre retention, missing-.prj assumption flag,
implausible rejection, NaN/inf/empty/inverted, EPSG:6933 fallback incl.
inclusive boundaries, axis order, projected->geographic conversion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest
from pyproj import CRS, Transformer
from shapely.geometry import box

from app.services.crs import resolve_source_crs, select_measurement_crs


# --- Minimal fakes mirroring app/services/readers.py interface ---

@dataclass
class FakeFeature:
    geometry: object
    index: int = 0
    properties: dict = field(default_factory=dict)


@dataclass
class FakeResult:
    features: list
    crs: CRS | None
    format: str


def _shapefile_result(geoms, crs):
    return FakeResult(
        features=[FakeFeature(g, i) for i, g in enumerate(geoms)],
        crs=crs,
        format="shapefile",
    )


def test_utm_northern_selection():
    crs, warnings = select_measurement_crs(CRS.from_epsg(4326), (10.0, 50.0, 11.0, 51.0))
    assert crs.to_epsg() == 32632  # lon ~10.5 -> zone 32N


def test_utm_southern_selection():
    crs, _ = select_measurement_crs(CRS.from_epsg(4326), (18.0, -34.0, 19.0, -33.0))
    assert crs.to_epsg() == 32734  # lon ~18.5 -> zone 34S


@pytest.mark.parametrize(
    "bounds,expected_zone",
    [
        ((-180.0, 0.0, -180.0, 1.0), 1),
        ((180.0, 0.0, 180.0, 1.0), 60),
        ((5.95, 0.0, 6.05, 1.0), 32),
        ((11.95, 0.0, 12.05, 1.0), 33),
    ],
)
def test_utm_longitude_boundaries(bounds, expected_zone):
    # Bounds stay inside [-180, 180]; centres are exactly the boundary
    # longitudes (-180/+180 use degenerate zero-width boxes, the only way
    # to centre exactly on the edge without leaving the valid range).
    crs, _ = select_measurement_crs(CRS.from_epsg(4326), bounds)
    assert crs.to_epsg() == 32600 + expected_zone


def test_projected_metre_crs_retained():
    source = CRS.from_epsg(32633)
    crs, warnings = select_measurement_crs(source, (500000, 0, 500100, 100))
    assert crs.to_epsg() == 32633


def test_missing_prj_assumes_4326_with_flag():
    geom = box(10.0, 50.0, 10.1, 50.1)  # plausible lon/lat
    result = _shapefile_result([geom], None)
    crs, assumed = resolve_source_crs(result)
    assert crs.to_epsg() == 4326
    assert assumed is True


def test_missing_prj_implausible_rejected():
    # Projected-looking eastings/northings must not be silently called lon/lat.
    geom = box(500000, 0, 500100, 100)
    result = _shapefile_result([geom], None)
    with pytest.raises(ValueError, match="cannot be resolved"):
        resolve_source_crs(result)


def test_missing_prj_no_usable_geoms_rejected():
    result = _shapefile_result([], None)
    with pytest.raises(ValueError, match="cannot be resolved"):
        resolve_source_crs(result)


def test_shapefile_with_prj_not_assumed():
    result = _shapefile_result([box(500000, 0, 500100, 100)], CRS.from_epsg(32633))
    crs, assumed = resolve_source_crs(result)
    assert crs.to_epsg() == 32633
    assert assumed is False


def test_kml_defaults_to_4326():
    result = FakeResult(features=[], crs=None, format="kml")
    crs, assumed = resolve_source_crs(result)
    assert crs.to_epsg() == 4326
    assert assumed is False


@pytest.mark.parametrize(
    "bounds",
    [
        (float("nan"), 0.0, 1.0, 1.0),
        (0.0, float("inf"), 1.0, 1.0),
        (0.0, float("-inf"), 1.0, 1.0),
        (),
        (1.0, 1.0, 0.0, 0.0),  # inverted
        (0.0, 1.0, 1.0, 0.0),  # inverted y
        (0.0, 0.0),  # wrong shape
        ("a", "b", "c", "d"),
    ],
)
def test_invalid_bounds_rejected(bounds):
    with pytest.raises(ValueError):
        select_measurement_crs(CRS.from_epsg(4326), bounds)


def test_resolve_rejects_nan_bounds():
    # Use a stub geometry: real Shapely constructors reject NaN rings at
    # build time, but readers may still surface NaN bounds from files.
    class NanGeom:
        is_empty = False
        bounds = (float("nan"), 0.0, 1.0, 1.0)

    result = _shapefile_result([NanGeom()], None)
    with pytest.raises(ValueError, match="invalid|resolved"):
        resolve_source_crs(result)


@pytest.mark.parametrize(
    "centre_lat,expected_epsg",
    [
        (85.0, 6933),
        (-81.0, 6933),
        (89.0, 6933),
        (50.0, 32631),  # sanity: normal lat still UTM (lon 0 -> zone 31)
    ],
)
def test_polar_fallback(centre_lat, expected_epsg):
    crs, warnings = select_measurement_crs(
        CRS.from_epsg(4326), (-0.5, centre_lat - 0.25, 0.5, centre_lat + 0.25)
    )
    assert crs.to_epsg() == expected_epsg
    if expected_epsg == 6933:
        assert any("6933" in w for w in warnings)


def test_polar_inclusive_boundaries_use_utm():
    # +84 and -80 are valid UTM latitudes (inclusive).
    crs_north, _ = select_measurement_crs(CRS.from_epsg(4326), (0.0, 83.9, 1.0, 84.1))
    assert crs_north.to_epsg() == 32631
    crs_south, _ = select_measurement_crs(CRS.from_epsg(4326), (0.0, -80.1, 1.0, -79.9))
    assert crs_south.to_epsg() == 32731


def test_just_outside_polar_boundaries_falls_back():
    crs_north, _ = select_measurement_crs(CRS.from_epsg(4326), (0.0, 84.0, 1.0, 84.5))
    # centre 84.25 > 84 -> fallback
    assert crs_north.to_epsg() == 6933
    crs_south, _ = select_measurement_crs(CRS.from_epsg(4326), (0.0, -80.5, 1.0, -80.0))
    # centre -80.25 < -80 -> fallback
    assert crs_south.to_epsg() == 6933


def test_utm_zone_math_lon10_lat50():
    # Zone-number math for already-geographic input: lon 10, lat 50 must
    # give zone 32N (32632). Note this input bypasses the app's
    # projected-to-geographic transformer (source is already EPSG:4326),
    # so transformer axis order is covered by
    # test_projected_non_metre_conversion_mandatory below, not here.
    crs, _ = select_measurement_crs(CRS.from_epsg(4326), (9.9, 49.9, 10.1, 50.1))
    assert crs.to_epsg() == 32632


def test_projected_bounds_converted_before_zone_selection():
    # Same geographic box (lon ~10, lat ~50) expressed in EPSG:3857 metres.
    # EPSG:3857 is metre-projected so it is retained per spec; the point is
    # that raw eastings (~1.1M) must never be read as longitude (which would
    # clamp to zone 60 / EPSG:32660). Retention proves the metre check ran on
    # CRS metadata, not on raw bound magnitudes. The non-metre conversion
    # path (which must select UTM from converted bounds) is covered by
    # test_projected_non_metre_conversion_mandatory.
    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_epsg(3857), always_xy=True
    )
    xmin, ymin, xmax, ymax = transformer.transform_bounds(9.9, 49.9, 10.1, 50.1)
    assert xmin > 1000  # sanity: these are metres, not degrees
    crs, _ = select_measurement_crs(
        CRS.from_epsg(3857), (xmin, ymin, xmax, ymax)
    )
    assert crs.to_epsg() == 3857
    # And the geographic conversion of those bounds centres on lon ~10:
    back = Transformer.from_crs(
        CRS.from_epsg(3857), CRS.from_epsg(4326), always_xy=True
    )
    gxmin, gymin, gxmax, gymax = back.transform_bounds(xmin, ymin, xmax, ymax)
    assert (gxmin + gxmax) / 2.0 == pytest.approx(10.0, abs=0.01)
    assert (gymin + gymax) / 2.0 == pytest.approx(50.0, abs=0.01)


def test_projected_non_metre_conversion_mandatory():
    # EPSG:3857 uses metres too, so use a genuinely non-metre CRS:
    # EPSG:2272 (NAD83 / Pennsylvania South, US survey foot).
    # This test round-trips through the app's production
    # projected-to-geographic transformer inside select_measurement_crs.
    # Coordinates are asymmetric (lon ~-75.1, lat ~40) so an axis swap is
    # detectable: correct conversion centres on (-75.1, 40.0) -> 32618,
    # while a transformer missing always_xy=True returns lat/lon-swapped
    # bounds centred on (40.0, -75.1) -> 32737, failing this test
    # (verified empirically: swapped output
    # (39.90, -75.2, 40.10, -75.0) selects 32737).
    src = CRS.from_epsg(2272)
    assert src.is_projected
    # A small box in that CRS near Philadelphia; convert a known lon/lat.
    geo = CRS.from_epsg(4326)
    to_src = Transformer.from_crs(geo, src, always_xy=True)
    xmin, ymin = to_src.transform(-75.2, 39.9)
    xmax, ymax = to_src.transform(-75.0, 40.1)
    lox, hix = (xmin, xmax) if xmin <= xmax else (xmax, xmin)
    loy, hiy = (ymin, ymax) if ymin <= ymax else (ymax, ymin)
    # Pin the fixture to Philadelphia lon/lat so the expected zone is auditable.
    check = Transformer.from_crs(src, geo, always_xy=True)
    gxmin, gymin, gxmax, gymax = check.transform_bounds(lox, loy, hix, hiy)
    assert (gxmin + gxmax) / 2.0 == pytest.approx(-75.1, abs=0.05)
    assert (gymin + gymax) / 2.0 == pytest.approx(40.0, abs=0.15)
    crs, _ = select_measurement_crs(src, (lox, loy, hix, hiy))
    # Philadelphia lon ~-75.1 -> zone 18N -> 32618
    assert crs.to_epsg() == 32618


def test_longitude_span_warning():
    _, warnings = select_measurement_crs(CRS.from_epsg(4326), (0.0, 10.0, 10.0, 11.0))
    assert any("6 degrees" in w for w in warnings)
    _, no_warn = select_measurement_crs(CRS.from_epsg(4326), (10.0, 50.0, 11.0, 51.0))
    assert no_warn == [] or not any("6 degrees" in w for w in no_warn)


def test_nan_centre_rejected():
    with pytest.raises(ValueError):
        select_measurement_crs(CRS.from_epsg(4326), (float("nan"), 0, 1, 1))
