"""Feature readers for shapefiles and KML.

Shapefiles and KML are read with ``pyogrio.raw.read`` (numpy arrays only),
so the runtime does not need GeoPandas or pyarrow. KML falls back to a
small secure lxml parser when the GDAL KML driver cannot read the file.

All parser errors are reported as :class:`ReadError` without internal
paths (only the file's base name is mentioned).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry.base import BaseGeometry


class ReadError(Exception):
    """A supported file could be opened but its features cannot be parsed."""


@dataclass
class RawFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict = field(default_factory=dict)


@dataclass
class ReadResult:
    features: list[RawFeature]
    crs: CRS | None
    format: str  # "shapefile" | "kml"


def _jsonable(value):
    """Normalize one attribute value into JSON-serializable data."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return None
        try:
            return str(value)
        except Exception:
            return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _decode_crs(crs_value, name: str) -> CRS | None:
    if crs_value is None:
        return None
    if isinstance(crs_value, CRS):
        return crs_value
    try:
        text = crs_value.decode() if isinstance(crs_value, bytes) else str(crs_value)
    except Exception as exc:
        raise ReadError(f"{name}: unreadable CRS metadata") from exc
    if not text.strip():
        return None
    try:
        return CRS.from_user_input(text)
    except Exception as exc:
        raise ReadError(f"{name}: invalid CRS metadata") from exc


def _geoms_from_wkb(wkb_array, name: str) -> list[BaseGeometry | None]:
    geoms: list[BaseGeometry | None] = []
    for element in wkb_array:
        if element is None:
            geoms.append(None)
            continue
        try:
            geoms.append(shapely.from_wkb(bytes(element)))
        except Exception as exc:
            raise ReadError(f"{name}: unreadable feature geometry") from exc
    return geoms


def _props_from_raw(meta: dict, field_arrays) -> list[dict]:
    names = [str(n) for n in meta.get("fields", [])]
    columns = [list(np.asarray(col).tolist()) for col in field_arrays]
    props: list[dict] = []
    n = len(columns[0]) if columns else 0
    # When there are no attribute columns, len is unknown here; caller pads.
    for i in range(n):
        props.append({k: _jsonable(col[i]) for k, col in zip(names, columns)})
    return props


def _read_pyogrio(path: Path, fmt: str) -> ReadResult:
    from pyogrio import raw as pyogrio_raw

    try:
        meta, _fids, wkb, field_arrays = pyogrio_raw.read(
            str(path), force_2d=True
        )
    except Exception as exc:
        raise ReadError(f"{path.name}: cannot parse {fmt} contents") from exc
    crs = _decode_crs(meta.get("crs"), path.name)
    geoms = _geoms_from_wkb(wkb if wkb is not None else [], path.name)
    props = _props_from_raw(meta, field_arrays)
    while len(props) < len(geoms):
        props.append({})
    features = [
        RawFeature(index=i, geometry=g, properties=props[i])
        for i, g in enumerate(geoms)
    ]
    return ReadResult(features=features, crs=crs, format=fmt)


def read_shapefile(path: Path) -> ReadResult:
    """Read an extracted ``.shp`` file (sidecars resolved by GDAL)."""
    return _read_pyogrio(Path(path), "shapefile")


def _read_kml_pyogrio_layers(path: Path) -> ReadResult:
    """Read KML across every GDAL layer (one per folder).

    The KML driver exposes each folder as a separate layer while the
    default read only returns the first, silently dropping placemarks.
    Indices are continuous across layers.
    """
    from pyogrio import list_layers
    from pyogrio import raw as pyogrio_raw

    try:
        layer_info = list_layers(str(path))
        count = len(layer_info) if layer_info is not None else 0
    except Exception as exc:
        raise ReadError(f"{path.name}: cannot parse kml contents") from exc
    if not count:
        return _read_pyogrio(path, "kml")
    features: list[RawFeature] = []
    crs: CRS | None = None
    # Iterate by numeric index: two folders may share the same name, so
    # name lookup would read the same layer twice and drop a placemark.
    for layer_index in range(count):
        try:
            meta, _fids, wkb, field_arrays = pyogrio_raw.read(
                str(path), layer=layer_index, force_2d=True
            )
        except Exception as exc:
            raise ReadError(f"{path.name}: cannot parse kml contents") from exc
        if crs is None:
            crs = _decode_crs(meta.get("crs"), path.name)
        geoms = _geoms_from_wkb(wkb if wkb is not None else [], path.name)
        props = _props_from_raw(meta, field_arrays)
        while len(props) < len(geoms):
            props.append({})
        for geom, prop in zip(geoms, props):
            features.append(
                RawFeature(index=len(features), geometry=geom, properties=prop)
            )
    return ReadResult(features=features, crs=crs, format="kml")


def read_kml(path: Path) -> ReadResult:
    """Read a standalone KML file (pyogrio first, secure lxml fallback)."""
    path = Path(path)
    try:
        return _read_kml_pyogrio_layers(path)
    except ReadError:
        pass
    except Exception:
        pass
    return _read_kml_lxml(path)


# --- Minimal secure KML fallback (Point / LineString / Polygon + holes) ---

_KML_PARSER_KWARGS = {
    "no_network": True,
    "resolve_entities": False,
    "load_dtd": False,
    "dtd_validation": False,
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.rsplit(":", 1)[-1]


def _findall_by_local(root, local_name: str) -> list:
    return [el for el in root.iter() if _local(el.tag) == local_name]


def _parse_kml_coords(text: str | None) -> list[tuple[float, float]] | None:
    if not text or not text.strip():
        return None
    coords: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            return None
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not (np.isfinite(lon) and np.isfinite(lat)):
            return None
        coords.append((lon, lat))
    return coords or None


def _placemark_props(pm) -> dict:
    props: dict = {}
    for child in pm:
        tag = _local(child.tag)
        if tag in ("name", "description", "styleUrl", "visibility"):
            if child.text and child.text.strip():
                props[tag] = child.text.strip()
        elif tag == "ExtendedData":
            for data in _findall_by_local(child, "Data"):
                key = data.get("name")
                val_el = next(
                    (c for c in data if _local(c.tag) == "value"), None
                )
                if key and val_el is not None and val_el.text:
                    props[key] = val_el.text.strip()
            for simple in _findall_by_local(child, "SimpleData"):
                key = simple.get("name")
                if key and simple.text and simple.text.strip():
                    props[key] = simple.text.strip()
    return {k: _jsonable(v) for k, v in props.items()}


def _placemark_geom(pm) -> BaseGeometry | None:
    geoms: list[BaseGeometry] = []
    for point in _findall_by_local(pm, "Point"):
        coord_el = next(
            (c for c in point.iter() if _local(c.tag) == "coordinates"), None
        )
        coords = _parse_kml_coords(
            coord_el.text if coord_el is not None else None
        )
        if coords:
            geoms.append(shapely.Point(coords[0]))
    for line in _findall_by_local(pm, "LineString"):
        coord_el = next(
            (c for c in line.iter() if _local(c.tag) == "coordinates"), None
        )
        coords = _parse_kml_coords(
            coord_el.text if coord_el is not None else None
        )
        if coords and len(coords) >= 2:
            geoms.append(shapely.LineString(coords))
    for poly in _findall_by_local(pm, "Polygon"):
        rings: list[list[tuple[float, float]]] = []
        for boundary_tag in ("outerBoundaryIs", "innerBoundaryIs"):
            for boundary in [
                c for c in poly.iter() if _local(c.tag) == boundary_tag
            ]:
                ring_el = next(
                    (
                        c
                        for c in boundary.iter()
                        if _local(c.tag) == "LinearRing"
                    ),
                    None,
                )
                coord_el = (
                    next(
                        (
                            c
                            for c in ring_el.iter()
                            if _local(c.tag) == "coordinates"
                        ),
                        None,
                    )
                    if ring_el is not None
                    else None
                )
                ring = _parse_kml_coords(
                    coord_el.text if coord_el is not None else None
                )
                if ring and len(ring) >= 3:
                    rings.append(ring)
        if rings:
            try:
                if len(rings) == 1:
                    geoms.append(shapely.Polygon(rings[0]))
                else:
                    geoms.append(shapely.Polygon(rings[0], rings[1:]))
            except Exception:
                return None
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    return shapely.GeometryCollection(geoms)


def _read_kml_lxml(path: Path) -> ReadResult:
    try:
        from lxml import etree
    except ImportError as exc:
        raise ReadError(f"{path.name}: cannot parse KML contents") from exc
    try:
        parser = etree.XMLParser(**_KML_PARSER_KWARGS)  # type: ignore[arg-type]
        tree = etree.parse(str(path), parser)
    except Exception as exc:
        raise ReadError(f"{path.name}: cannot parse KML contents") from exc
    root = tree.getroot()
    placemarks = _findall_by_local(root, "Placemark")
    # A well-formed KML document with zero placemarks is an empty dataset,
    # not a parse error.
    features = [
        RawFeature(
            index=i,
            geometry=_placemark_geom(pm),
            properties=_placemark_props(pm),
        )
        for i, pm in enumerate(placemarks)
    ]
    return ReadResult(features=features, crs=CRS.from_epsg(4326), format="kml")
