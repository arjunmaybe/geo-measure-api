"""Geometry preparation and classification.

``prepare`` normalizes one raw geometry for measurement: missing/empty
input stays ``None`` (feature becomes EMPTY downstream), invalid geometry
is repaired with Shapely's ``make_valid`` when possible, and an
unrepairable geometry yields ``(None, True, message)`` so the caller marks
the feature ERROR instead of failing the whole upload.

``classify`` maps a prepared geometry to a measurement kind. The caller
reclassifies the *repaired* output, since repair can change the type; a
mixed GeometryCollection may then classify as unsupported, so polygonal
area it contains may not be measured.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

_AREA_TYPES = {"Polygon", "MultiPolygon"}
_LENGTH_TYPES = {"LineString", "MultiLineString"}
_POINT_TYPES = {"Point", "MultiPoint"}


def classify(geom) -> str:
    """Return 'area', 'length', 'none', or 'unsupported'."""
    if geom is None or getattr(geom, "is_empty", False):
        return "none"
    gtype = getattr(geom, "geom_type", None)
    if gtype in _AREA_TYPES:
        return "area"
    if gtype in _LENGTH_TYPES:
        return "length"
    if gtype in _POINT_TYPES:
        return "none"
    return "unsupported"


def prepare(
    geom,
) -> tuple[BaseGeometry | None, bool, str | None]:
    """Prepare one geometry; return (geometry, repaired, message).

    - missing/empty -> (None, False, message); caller maps to EMPTY.
    - valid -> (geom, False, None).
    - repaired -> (fixed, True, message); caller reclassifies `fixed`.
    - unrepairable -> (None, True, message); caller maps to ERROR.
    """
    if geom is None:
        return None, False, "missing geometry"
    try:
        if geom.is_empty:
            return None, False, "empty geometry"
    except Exception:
        return None, False, "unreadable geometry"
    try:
        valid = bool(geom.is_valid)
    except Exception:
        valid = False
    if valid:
        return geom, False, None
    try:
        fixed = make_valid(geom)
    except Exception as exc:
        return None, True, f"geometry repair failed: {exc}"
    if fixed is None or getattr(fixed, "is_empty", False):
        return None, True, "geometry repair produced empty geometry"
    try:
        still_valid = bool(fixed.is_valid)
    except Exception:
        still_valid = False
    if not still_valid:
        return None, True, "geometry could not be repaired"
    return fixed, True, "geometry repaired with make_valid"
