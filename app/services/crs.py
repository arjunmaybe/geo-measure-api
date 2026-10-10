"""CRS resolution and measurement-CRS selection.

All pyproj transformers in this module use ``always_xy=True`` so axis order
is always longitude/latitude (x/y), regardless of the CRS definition order.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pyproj import CRS, Transformer

if TYPE_CHECKING:  # avoid a runtime dependency on readers (Phase 2)
    from app.services.readers import ReadResult

_GEO_CRS = CRS.from_epsg(4326)
_POLAR_CRS = CRS.from_epsg(6933)

_UTM_LAT_MIN = -80.0
_UTM_LAT_MAX = 84.0

_LON_MIN = -180.0
_LON_MAX = 180.0
_LAT_MIN = -90.0
_LAT_MAX = 90.0


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _validate_bounds(bounds: tuple) -> tuple[float, float, float, float]:
    """Validate a (minx, miny, maxx, maxy) bound in any CRS.

    Raises ValueError for wrong shape, NaN/inf, empty, or inverted bounds.
    """
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 4:
        raise ValueError(f"bounds must be a 4-tuple, got {bounds!r}")
    try:
        minx, miny, maxx, maxy = (float(v) for v in bounds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bounds must contain numbers, got {bounds!r}") from exc
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise ValueError(f"bounds must be finite, got {bounds!r}")
    if minx > maxx or miny > maxy:
        raise ValueError(f"bounds are inverted or empty, got {bounds!r}")
    if minx == maxx and miny == maxy:
        # A degenerate point bound is technically usable for UTM selection
        # (centre is well-defined), so allow it. Truly "empty" datasets
        # (no usable bounds at all) are rejected by the caller passing no
        # bounds / the resolve step raising first.
        pass
    return minx, miny, maxx, maxy


def _is_plausible_lonlat_bounds(bounds: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bounds
    return (
        _LON_MIN <= minx <= _LON_MAX
        and _LON_MIN <= maxx <= _LON_MAX
        and _LAT_MIN <= miny <= _LAT_MAX
        and _LAT_MIN <= maxy <= _LAT_MAX
    )


def _is_metre_projected(crs: CRS) -> bool:
    """True when a projected CRS uses metre linear units.

    Checks axis metadata rather than guessing from bounds values, so raw
    eastings/northings are never mistaken for longitude/latitude.
    """
    try:
        if not crs.is_projected:
            return False
    except Exception:
        return False
    try:
        axes = crs.axis_info
    except Exception:
        return False
    if not axes:
        return False
    for axis in axes:
        unit = (getattr(axis, "unit_name", "") or "").lower()
        # pyproj reports "metre"; accept "meter"/"metre(s)" spellings.
        if unit not in ("metre", "meter", "metres", "meters"):
            return False
    return True


def _to_geographic_bounds(
    source: CRS, bounds: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Convert bounds from the source CRS to EPSG:4326 lon/lat."""
    if source == _GEO_CRS or (source.is_geographic and source.to_epsg() == 4326):
        return bounds
    try:
        transformer = Transformer.from_crs(source, _GEO_CRS, always_xy=True)
        gminx, gminy, gmaxx, gmaxy = transformer.transform_bounds(*bounds)
    except Exception as exc:
        raise ValueError(f"cannot convert bounds to geographic: {exc}") from exc
    if not all(math.isfinite(v) for v in (gminx, gminy, gmaxx, gmaxy)):
        raise ValueError(f"projected bounds convert to non-finite geographic bounds")
    return gminx, gminy, gmaxx, gmaxy


def resolve_source_crs(result: ReadResult) -> tuple[CRS, bool]:
    """Resolve the source CRS for a reader result.

    - KML defaults to EPSG:4326 (assumed=False).
    - Shapefiles use their .prj CRS when available (assumed=False).
    - Missing .prj: assume EPSG:4326 (assumed=True) only if every usable
      geometry bound is plausible lon/lat; otherwise raise ValueError.
    - NaN/inf/inverted/empty bounds are rejected, never silently assigned.
    """
    fmt = getattr(result, "format", None)
    crs = getattr(result, "crs", None)

    if fmt == "kml":
        return CRS.from_epsg(4326), False

    if crs is not None:
        if not isinstance(crs, CRS):
            try:
                crs = CRS.from_user_input(crs)
            except Exception as exc:
                raise ValueError(f"invalid source CRS: {exc}") from exc
        return crs, False

    # No CRS metadata: only shapefiles without .prj reach here.
    features = getattr(result, "features", None) or []
    usable: list[tuple[float, float, float, float]] = []
    for feature in features:
        geom = getattr(feature, "geometry", None)
        if geom is None or getattr(geom, "is_empty", False):
            continue
        try:
            b = geom.bounds
        except Exception:
            continue
        if b is None:
            continue
        try:
            validated = _validate_bounds(tuple(b))
        except ValueError:
            raise ValueError(
                "source CRS cannot be resolved: "
                "geometry bounds are invalid (NaN/inf/empty/inverted)"
            )
        usable.append(validated)

    if not usable:
        raise ValueError(
            "source CRS cannot be resolved: missing .prj and no usable "
            "geometry bounds to assume EPSG:4326 from"
        )
    for b in usable:
        if not _is_plausible_lonlat_bounds(b):
            raise ValueError(
                "source CRS cannot be resolved: missing .prj and geometry "
                "bounds are not plausible longitude/latitude coordinates"
            )
    return CRS.from_epsg(4326), True


def select_measurement_crs(
    source: CRS, bounds: tuple
) -> tuple[CRS, list[str]]:
    """Select one measurement CRS for a file.

    - Validates bounds (finite, non-inverted).
    - Retains a projected source CRS whose linear units are metres.
    - Otherwise selects UTM from the geographic bbox centre, or EPSG:6933
      when the centre latitude is outside [-80, +84] (inclusive).
    - Warns when the geographic longitude span exceeds six degrees.
    - Projected bounds are converted to geographic before zone selection;
      raw eastings/northings are never used as lon/lat.
    """
    if not isinstance(source, CRS):
        try:
            source = CRS.from_user_input(source)
        except Exception as exc:
            raise ValueError(f"invalid source CRS: {exc}") from exc

    validated = _validate_bounds(tuple(bounds))
    geo_bounds = _to_geographic_bounds(source, validated)
    gminx, gminy, gmaxx, gmaxy = geo_bounds

    warnings: list[str] = []
    lon_span = gmaxx - gminx
    if lon_span > 6.0:
        warnings.append(
            f"longitude span {lon_span:.2f} degrees exceeds 6 degrees; "
            "a single UTM zone may distort measurements far from the "
            "central meridian"
        )

    # Retain metre-based projected CRSs (no reprojection needed).
    if _is_metre_projected(source):
        return source, warnings

    centre_lon = (gminx + gmaxx) / 2.0
    centre_lat = (gminy + gmaxy) / 2.0
    if not (math.isfinite(centre_lon) and math.isfinite(centre_lat)):
        raise ValueError("cannot select measurement CRS: non-finite bbox centre")

    # Polar fallback depends on the bbox *centre*, so a dataset may extend
    # beyond the UTM latitude range while its centre stays inside.
    if centre_lat < _UTM_LAT_MIN or centre_lat > _UTM_LAT_MAX:
        warnings.append(
            f"bbox centre latitude {centre_lat:.4f} is outside the UTM range "
            f"[{_UTM_LAT_MIN:.0f}, {_UTM_LAT_MAX:.0f}]; using EPSG:6933"
        )
        return CRS.from_epsg(6933), warnings

    # Clamp longitude into [-180, 180] for zone math, then clamp zone 1-60.
    clamped_lon = min(max(centre_lon, -180.0), 180.0)
    zone = int(math.floor((clamped_lon + 180.0) / 6.0) + 1)
    zone = min(max(zone, 1), 60)
    epsg = 32600 + zone if centre_lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg), warnings
