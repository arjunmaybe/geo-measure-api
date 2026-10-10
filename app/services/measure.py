"""Area/length measurement on already-projected geometries.

This function accepts geometry already transformed into a projected CRS with
metre-based units. It cannot infer the CRS from a Shapely object, so callers
must reproject first; measuring raw longitude/latitude degrees would produce
meaningless "square degrees".
"""

from __future__ import annotations

import math


def measure(geom_projected, kind: str) -> dict | None:
    """Measure a projected geometry.

    - kind "area" -> {"type": "area", "value": float, "unit": "m2"}
    - kind "length" -> {"type": "length", "value": float, "unit": "m"}
    - kind "none" / "unsupported" -> None (measurement does not apply)
    - any other kind -> ValueError (explicit unsupported-kind handling)
    - None / empty geometries -> None
    - non-finite results -> ValueError
    - always returns built-in Python floats, never NumPy scalars.
    """
    if kind not in ("area", "length", "none", "unsupported"):
        raise ValueError(f"unsupported measurement kind: {kind!r}")
    if kind in ("none", "unsupported"):
        return None
    if geom_projected is None or getattr(geom_projected, "is_empty", False):
        return None

    if kind == "area":
        raw = geom_projected.area
        unit = "m2"
    else:  # kind == "length"
        raw = geom_projected.length
        unit = "m"

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"measurement produced non-numeric value: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"measurement produced non-finite value: {value!r}")
    return {"type": kind, "value": value, "unit": unit}
