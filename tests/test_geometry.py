"""Unit tests for the prepare/classify repair contract (geometry.py).

Locks the documented behavior: prepare() normalizes or repairs one
geometry, and the caller reclassifies the *repaired* geometry (repair can
change the type), so measurement kinds always reflect the final shape.
"""

from __future__ import annotations

from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from app.services.geometry import classify, prepare


def test_classify_kinds():
    assert classify(Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])) == "area"
    assert classify(MultiPolygon([
        Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        Polygon([(2, 2), (3, 2), (3, 3), (2, 3)]),
    ])) == "area"
    assert classify(LineString([(0, 0), (1, 1)])) == "length"
    assert classify(Point(0, 0)) == "none"
    assert classify(None) == "none"
    assert classify(Polygon()) == "none"


def test_prepare_valid_passthrough():
    geom = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    fixed, repaired, message = prepare(geom)
    assert fixed is geom
    assert repaired is False
    assert message is None


def test_prepare_missing_and_empty():
    assert prepare(None) == (None, False, "missing geometry")
    geom, repaired, _ = prepare(Polygon())
    assert geom is None and repaired is False


def test_prepare_repairs_bowtie_and_reclassifies():
    # Self-intersecting bowtie: invalid input is repaired, and the repaired
    # geometry reclassifies to area (not left as the invalid original).
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    assert not bowtie.is_valid
    fixed, repaired, message = prepare(bowtie)
    assert repaired is True
    assert fixed is not None and fixed.is_valid
    assert message
    assert classify(fixed) == "area"
