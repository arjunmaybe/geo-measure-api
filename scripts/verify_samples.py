"""Verify generated samples with the real app readers (Phase 2 gate).

The shapefile archive goes through the application's own safe_extract(),
so the sample is verified under the same safety checks as real uploads.
"""

import json
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from app.services.ingest import safe_extract
from app.services.readers import read_kml, read_shapefile

kml = read_kml(root / "data" / "sample.kml")
print("KML crs:", kml.crs, "| format:", kml.format, "| n:", len(kml.features))
for f in kml.features:
    g = f.geometry
    print(
        f"  idx={f.index} type={getattr(g, 'geom_type', None)} "
        f"area={getattr(g, 'area', None)} len={getattr(g, 'length', None)} "
        f"props={json.dumps(f.properties, sort_keys=True)}"
    )
    if g is not None and g.geom_type == "Polygon":
        print("    interiors:", len(g.interiors))

with tempfile.TemporaryDirectory() as tmp:
    shp = safe_extract(
        (root / "data" / "sample-shapefile.zip").read_bytes(),
        Path(tmp) / "extract",
    )
    shp_res = read_shapefile(shp)
print("SHP crs:", shp_res.crs, "| format:", shp_res.format, "| n:", len(shp_res.features))
for f in shp_res.features:
    g = f.geometry
    print(
        f"  idx={f.index} type={getattr(g, 'geom_type', None)} "
        f"area={getattr(g, 'area', None)} props={json.dumps(f.properties, sort_keys=True)}"
    )
