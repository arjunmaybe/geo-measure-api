"""Deterministic sample-data generator.

Writes ``data/sample.kml`` (plain KML XML text; the GDAL KML driver is not
assumed to support writing) and ``data/sample-shapefile.zip`` (ESRI
Shapefile sidecars zipped deterministically).

Sample-generation-only dependency: GeoPandas (for shapefile writing).
It is NOT a runtime dependency — ``app/`` never imports it::

    pip install geopandas

All coordinates are fixed (EPSG:4326, near lon 10 / lat 50) so every run
produces the same geometries and measurements.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Fixed sample coordinates (lon, lat).
SQUARE_OUTER = [
    (10.0, 50.0),
    (10.001, 50.0),
    (10.001, 50.001),
    (10.0, 50.001),
    (10.0, 50.0),
]
SQUARE_HOLE = [
    (10.0004, 50.0004),
    (10.0006, 50.0004),
    (10.0006, 50.0006),
    (10.0004, 50.0006),
    (10.0004, 50.0004),
]
TRIANGLE = [
    (10.0, 50.0),
    (10.002, 50.0),
    (10.0, 50.002),
    (10.0, 50.0),
]
ROAD = [(10.0, 50.0), (10.002, 50.0)]
WELL = (10.0, 50.0)


def _ring_text(ring) -> str:
    return " ".join(f"{x:.6f},{y:.6f},0" for x, y in ring)


def build_kml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>geo-measure-api sample</name>
    <description>Deterministic sample: polygon with hole, road, well.</description>
    <Placemark>
      <name>square</name>
      <description>Approx 111 by 71 m square with a courtyard hole</description>
      <ExtendedData>
        <Data name="kind"><value>garden</value></Data>
      </ExtendedData>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>{_ring_text(SQUARE_OUTER)}</coordinates>
          </LinearRing>
        </outerBoundaryIs>
        <innerBoundaryIs>
          <LinearRing>
            <coordinates>{_ring_text(SQUARE_HOLE)}</coordinates>
          </LinearRing>
        </innerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>road</name>
      <description>Approx 143 m road segment</description>
      <LineString>
        <coordinates>{_ring_text(ROAD)}</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>well</name>
      <description>Point feature: no area/length applies</description>
      <Point>
        <coordinates>{WELL[0]:.6f},{WELL[1]:.6f},0</coordinates>
      </Point>
    </Placemark>
  </Document>
</kml>
"""


def write_kml() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "sample.kml"
    out.write_text(build_kml(), encoding="utf-8")
    return out


def write_shapefile_zip() -> Path:
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon
    except ImportError:
        sys.exit("sample shapefile generation requires GeoPandas: pip install geopandas")
    import tempfile

    # Shapefiles hold a single geometry type: two polygons.
    lot_a = Polygon(SQUARE_OUTER)
    lot_b = Polygon(TRIANGLE)
    gdf = gpd.GeoDataFrame(
        {"name": ["lot_a", "lot_b"], "kind": ["parcel", "parcel"]},
        geometry=[lot_a, lot_b],
        crs="EPSG:4326",
    )
    allowed = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    with tempfile.TemporaryDirectory(prefix="geo_sample_") as tmp:
        tmpdir = Path(tmp)
        gdf.to_file(tmpdir / "sample.shp", driver="ESRI Shapefile")
        members = sorted(
            p for p in tmpdir.iterdir()
            if p.is_file() and p.suffix.lower() in allowed
        )
        stems = {p.stem for p in members}
        assert len(stems) == 1, f"expected one shapefile stem, got {stems}"
        assert {"sample.shp", "sample.shx", "sample.dbf"} <= {
            p.name for p in members
        }, f"missing required sidecars: {[p.name for p in members]}"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = DATA_DIR / "sample-shapefile.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in members:
                # Fixed timestamp keeps repeated runs byte-deterministic.
                info = zipfile.ZipInfo(member.name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, member.read_bytes())
    return out


def main() -> None:
    kml = write_kml()
    shp_zip = write_shapefile_zip()
    print(f"wrote {kml} ({kml.stat().st_size} bytes)")
    print(f"wrote {shp_zip} ({shp_zip.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
