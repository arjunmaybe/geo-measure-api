"""Ingestion tests (Phase 2): extraction safety, limits, statuses, isolation.

All shapefile fixtures are built in-test with a minimal pure-Python writer
(no GeoPandas), so this suite runs on a fresh install with only
``requirements.txt``. The public ``data/`` samples are used for the
happy-path cases only.
"""

from __future__ import annotations

import io
import json
import stat
import struct
import tempfile
import zipfile
from pathlib import Path

import pytest
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Polygon
from shapely.ops import transform as shp_transform
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import init_db, make_engine
from app.models import Feature, File
from app.services import ingest
from app.services.ingest import (
    FileTooLargeError,
    InvalidArchiveError,
    UnsupportedTypeError,
    process_upload,
    safe_extract,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Fixed sample coordinates (mirror scripts/generate_sample_data.py).
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
ROAD = [(10.0, 50.0), (10.002, 50.0)]

EMPTY_KML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
    "  <Document><name>empty</name></Document>\n"
    "</kml>\n"
).encode()


# --- fixtures ---


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def sample_zip_bytes():
    return (DATA_DIR / "sample-shapefile.zip").read_bytes()


@pytest.fixture()
def sample_kml_bytes():
    return (DATA_DIR / "sample.kml").read_bytes()


def file_count(db) -> int:
    return db.query(File).count()


# --- minimal shapefile writer (test-only, no GeoPandas) ---


def _write_minimal_shapefile(directory: Path, stem: str, polygons, names=None):
    """Write .shp/.shx/.dbf for polygon features (no .prj).

    polygons: list of features; each feature is a list of rings;
    each ring is a list of (x, y) tuples.
    """
    records = []
    all_pts = []
    for rings in polygons:
        pts: list[tuple[float, float]] = []
        parts: list[int] = []
        for ring in rings:
            parts.append(len(pts))
            pts.extend([(float(x), float(y)) for x, y in ring])
        all_pts.extend(pts)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        content = struct.pack("<i4d", 5, min(xs), min(ys), max(xs), max(ys))
        content += struct.pack("<ii", len(rings), len(pts))
        content += struct.pack(f"<{len(parts)}i", *parts)
        flat = [c for p in pts for c in p]
        content += struct.pack(f"<{len(flat)}d", *flat)
        records.append(content)
    if all_pts:
        bbox = (
            min(p[0] for p in all_pts),
            min(p[1] for p in all_pts),
            max(p[0] for p in all_pts),
            max(p[1] for p in all_pts),
        )
        shape_type = 5
    else:
        bbox = (0.0, 0.0, 0.0, 0.0)
        shape_type = 0

    shp_len = 50 + sum(4 + len(c) // 2 for c in records)
    header = struct.pack(">i5i", 9994, 0, 0, 0, 0, 0)
    header += struct.pack(">i", shp_len)
    header += struct.pack("<ii", 1000, shape_type)
    header += struct.pack("<8d", *bbox, 0.0, 0.0, 0.0, 0.0)
    body = b""
    shx_entries = []
    offset = 50
    for i, content in enumerate(records, start=1):
        words = len(content) // 2
        body += struct.pack(">ii", i, words) + content
        shx_entries.append((offset, words))
        offset += 4 + words
    (directory / f"{stem}.shp").write_bytes(header + body)

    shx = struct.pack(">i5i", 9994, 0, 0, 0, 0, 0)
    shx += struct.pack(">i", 50 + 4 * len(records))
    shx += struct.pack("<ii", 1000, shape_type)
    shx += struct.pack("<8d", *bbox, 0.0, 0.0, 0.0, 0.0)
    for off, words in shx_entries:
        shx += struct.pack(">ii", off, words)
    (directory / f"{stem}.shx").write_bytes(shx)

    names = names or [f"feat{i}" for i in range(len(records))]
    nrec = len(records)
    dbf = struct.pack("<BBBBihh20s", 0x03, 120, 1, 1, nrec, 65, 21, b"\0" * 20)
    dbf += struct.pack("<11sc4sBB14s", b"NAME", b"C", b"\0" * 4, 20, 0, b"\0" * 14)
    dbf += b"\x0d"
    for name in names:
        dbf += b" " + name.encode("ascii")[:20].ljust(20)
    (directory / f"{stem}.dbf").write_bytes(dbf)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in members.items():
            zf.writestr(arcname, content)
    return buf.getvalue()


def _projected_no_prj_zip(tmp_path: Path) -> bytes:
    """Test-only unresolved-CRS fixture: projected coords, no .prj.

    Deliberately NOT placed in data/ (it would be a misleading sample).
    """
    d = tmp_path / "noprj"
    d.mkdir()
    _write_minimal_shapefile(
        d,
        "projected",
        [[[(500000.0, 0.0), (500100.0, 0.0), (500100.0, 100.0),
           (500000.0, 100.0), (500000.0, 0.0)]]],
        names=["block"],
    )
    return _zip_bytes({
        "projected.shp": (d / "projected.shp").read_bytes(),
        "projected.shx": (d / "projected.shx").read_bytes(),
        "projected.dbf": (d / "projected.dbf").read_bytes(),
    })


# --- invalid archives: no file record may remain ---


def test_zip_traversal_rejected_no_record(db, tmp_path):
    payload = _zip_bytes({
        "../../evil.shp": b"evil",
        "a.shp": b"a",
        "a.shx": b"b",
        "a.dbf": b"c",
    })
    with pytest.raises(InvalidArchiveError):
        process_upload("evil.zip", payload, db)
    assert file_count(db) == 0


@pytest.mark.parametrize("arcname", ["../evil.shp", "sub/../../evil.shp", "/abs.shp"])
def test_zip_traversal_variants_no_record(db, arcname):
    payload = _zip_bytes({arcname: b"evil"})
    with pytest.raises(InvalidArchiveError):
        safe_extract(payload, Path(tempfile.mkdtemp()))
    assert file_count(db) == 0


def test_zip_traversal_directory_entry_no_record(db, sample_zip_bytes):
    # Traversal-style directory listed AFTER an otherwise valid shapefile
    # must still be rejected: member paths are validated before directory
    # entries are skipped.
    with zipfile.ZipFile(io.BytesIO(sample_zip_bytes)) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    members["../../evil_dir/"] = b""
    with pytest.raises(InvalidArchiveError, match="unsafe"):
        process_upload("sample-shapefile.zip", _zip_bytes(members), db)
    assert file_count(db) == 0


def test_mismatched_sidecar_directories_no_record(db, tmp_path):
    # Same stem in different folders is not a shapefile: sidecars must
    # share the .shp's relative directory and stem.
    d = tmp_path / "split"
    d.mkdir()
    _write_minimal_shapefile(
        d,
        "t",
        [[[(1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 1.0)]]],
        names=["t"],
    )
    payload = _zip_bytes({
        "a/t.shp": (d / "t.shp").read_bytes(),
        "b/t.shx": (d / "t.shx").read_bytes(),
        "b/t.dbf": (d / "t.dbf").read_bytes(),
    })
    with pytest.raises(InvalidArchiveError, match="sidecar"):
        process_upload("split.zip", payload, db)
    assert file_count(db) == 0


def test_zip_symlink_entry_no_record(db, sample_zip_bytes):
    # Symlink listed after valid members: the whole archive is scanned,
    # nothing is persisted, and partial output is cleaned.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(io.BytesIO(sample_zip_bytes)) as src:
            for name in src.namelist():
                zf.writestr(name, src.read(name))
        link = zipfile.ZipInfo("link.shp")
        link.create_system = 3  # Unix attributes carry the file type
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "sample.shp")
    with pytest.raises(InvalidArchiveError, match="symlink"):
        process_upload("sample-shapefile.zip", buf.getvalue(), db)
    assert file_count(db) == 0


def test_member_count_limit_no_record(db):
    members = {f"f{i:03d}.txt": b"x" for i in range(51)}
    with pytest.raises(InvalidArchiveError, match="limit"):
        process_upload("many.zip", _zip_bytes(members), db)
    assert file_count(db) == 0


def test_declared_size_limit_no_record(db, monkeypatch, sample_zip_bytes):
    monkeypatch.setattr(config, "MAX_UNCOMPRESSED_MB", 0.000001)
    with pytest.raises(InvalidArchiveError, match="declared"):
        process_upload("sample-shapefile.zip", sample_zip_bytes, db)
    assert file_count(db) == 0


def test_actual_decompressed_limit_no_record(db, monkeypatch, tmp_path):
    # Simulate a decompression bomb: central-directory sizes are honest and
    # small (declared check passes) but the stream yields far more bytes, so
    # only the streaming byte counter aborts, mid-extraction.
    d = tmp_path / "tiny"
    d.mkdir()
    _write_minimal_shapefile(
        d,
        "t",
        [[[(1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 1.0)]]],
        names=["t"],
    )
    payload = _zip_bytes({
        "t.shp": (d / "t.shp").read_bytes(),
        "t.shx": (d / "t.shx").read_bytes(),
        "t.dbf": (d / "t.dbf").read_bytes(),
    })
    orig_open = zipfile.ZipFile.open

    def _bomb_open(self, name, mode="r", *args, **kwargs):
        handle = orig_open(self, name, mode, *args, **kwargs)
        if mode == "r":
            data = handle.read()
            handle.close()
            return io.BytesIO(data + b"0" * 100000)
        return handle

    monkeypatch.setattr(zipfile.ZipFile, "open", _bomb_open)
    monkeypatch.setattr(config, "MAX_UNCOMPRESSED_MB", 0.0005)
    dest = tmp_path / "extract"
    with pytest.raises(InvalidArchiveError, match="decompressed"):
        safe_extract(payload, dest)
    assert list(dest.iterdir()) == []  # partial output removed
    with pytest.raises(InvalidArchiveError):
        process_upload("t.zip", payload, db)
    assert file_count(db) == 0


def test_missing_sidecars_no_record(db, sample_zip_bytes, tmp_path):
    with zipfile.ZipFile(io.BytesIO(sample_zip_bytes)) as zf:
        members = {
            name: zf.read(name)
            for name in zf.namelist()
            if not name.lower().endswith(".dbf")
        }
    assert any(n.lower().endswith(".shp") for n in members)
    with pytest.raises(InvalidArchiveError, match="sidecar"):
        process_upload("sample-shapefile.zip", _zip_bytes(members), db)
    assert file_count(db) == 0


def test_wrong_extension_no_record(db):
    with pytest.raises(UnsupportedTypeError):
        process_upload("notes.txt", b"hello", db)
    assert file_count(db) == 0


def test_corrupt_zip_no_record(db):
    with pytest.raises(InvalidArchiveError):
        process_upload("broken.zip", b"\x00\x01\x02 not a zip", db)
    assert file_count(db) == 0


def test_oversized_upload_rejected(db):
    big = b"x" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    with pytest.raises(FileTooLargeError):
        process_upload("big.zip", big, db)
    assert file_count(db) == 0


# --- supported but unparsable / unresolvable: persisted FAILED ---


def test_unreadable_kml_persisted_failed(db):
    record = process_upload("junk.kml", b"\x00\x01\x02 binary garbage", db)
    assert record.status == "FAILED"
    assert record.error
    assert file_count(db) == 1
    assert db.query(Feature).count() == 0


def test_malformed_kml_xml_persisted_failed(db):
    record = process_upload("bad.kml", b"<kml><unclosed>", db)
    assert record.status == "FAILED"
    assert record.error
    assert file_count(db) == 1


def test_unreadable_shapefile_persisted_failed(db):
    payload = _zip_bytes({
        "t.shp": b"garbage" * 16,
        "t.shx": b"garbage" * 4,
        "t.dbf": b"garbage" * 4,
    })
    record = process_upload("t.zip", payload, db)
    assert record.status == "FAILED"
    assert record.error
    assert file_count(db) == 1


def test_unresolved_crs_persisted_failed(db, tmp_path):
    payload = _projected_no_prj_zip(tmp_path)
    record = process_upload("projected.zip", payload, db)
    assert record.status == "FAILED"
    assert record.error and "resolve" in record.error.lower()
    assert file_count(db) == 1
    assert record.feature_count == 0


# --- happy paths: statuses, values, isolation ---


def _reference_area_32632() -> float:
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32632", always_xy=True)
    square = Polygon(SQUARE_OUTER, [SQUARE_HOLE])
    return shp_transform(to_utm.transform, square).area


def _reference_length_32632() -> float:
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32632", always_xy=True)
    return shp_transform(to_utm.transform, LineString(ROAD)).length


def test_kml_sample_ingest(db, sample_kml_bytes):
    record = process_upload("sample.kml", sample_kml_bytes, db)
    assert record.status == "COMPLETED"
    assert record.error is None
    assert record.source_crs == "EPSG:4326"
    assert record.measurement_crs == "EPSG:32632"
    assert record.crs_assumed is False
    assert record.feature_count == 3

    feats = (
        db.query(Feature).filter_by(file_id=record.id).order_by(Feature.idx).all()
    )
    assert len(feats) == 3
    by_name = {}
    for feat in feats:
        json.dumps(feat.properties)  # must be JSON-compatible
        assert feat.geometry_geojson is not None
        name = feat.properties.get("Name", feat.properties.get("name"))
        by_name[name] = feat

    square = by_name["square"]
    assert square.status == "MEASURED"
    assert square.measure_type == "area" and square.measure_unit == "m2"
    assert square.measure_value == pytest.approx(_reference_area_32632(), rel=1e-6)
    assert square.geometry_type == "Polygon"
    assert square.geometry_repaired is False

    road = by_name["road"]
    assert road.status == "MEASURED"
    assert road.measure_type == "length" and road.measure_unit == "m"
    assert road.measure_value == pytest.approx(_reference_length_32632(), rel=1e-6)

    well = by_name["well"]
    assert well.status == "NOT_APPLICABLE"
    assert well.measure_type is None and well.measure_value is None
    assert well.geometry_type == "Point"


def test_shapefile_sample_ingest(db, sample_zip_bytes):
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32632", always_xy=True)
    record = process_upload("sample-shapefile.zip", sample_zip_bytes, db)
    assert record.status == "COMPLETED"
    assert record.source_crs == "EPSG:4326"
    assert record.measurement_crs == "EPSG:32632"
    assert record.crs_assumed is False
    assert record.feature_count == 2

    feats = (
        db.query(Feature).filter_by(file_id=record.id).order_by(Feature.idx).all()
    )
    assert [f.properties["name"] for f in feats] == ["lot_a", "lot_b"]
    for feat in feats:
        assert feat.status == "MEASURED"
        assert feat.measure_type == "area" and feat.measure_unit == "m2"
        assert isinstance(feat.measure_value, float)
        json.dumps(feat.geometry_geojson)

    lot_a = feats[0]
    ref = shp_transform(
        to_utm.transform, Polygon(SQUARE_OUTER)
    ).area
    assert lot_a.measure_value == pytest.approx(ref, rel=1e-6)


def test_one_bad_feature_does_not_abort(db, sample_kml_bytes, monkeypatch):
    from app.services import measure as measure_module

    calls = {"n": 0}
    real_measure = measure_module.measure

    def flaky(geom, kind):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated boom")
        return real_measure(geom, kind)

    monkeypatch.setattr(measure_module, "measure", flaky)
    record = process_upload("sample.kml", sample_kml_bytes, db)
    assert record.status == "COMPLETED"
    feats = (
        db.query(Feature).filter_by(file_id=record.id).order_by(Feature.idx).all()
    )
    assert [f.status for f in feats] == ["MEASURED", "ERROR", "NOT_APPLICABLE"]
    assert feats[1].message and "boom" in feats[1].message


def test_zero_features_completed_null_crs(db):
    record = process_upload("empty.kml", EMPTY_KML, db)
    assert record.status == "COMPLETED"
    assert record.feature_count == 0
    assert record.source_crs == "EPSG:4326"
    assert record.measurement_crs is None
    assert db.query(Feature).filter_by(file_id=record.id).count() == 0


def test_unusable_bounds_feature_is_error_not_measured(db, tmp_path, monkeypatch):
    # Regression: a readable file whose only non-empty feature has
    # non-finite bounds (resolvable CRS, no usable bounds anywhere) must
    # not be measured in raw source coordinates. The feature is ERROR with
    # no value/unit, the file still completes, and no CRS is fabricated.
    from app.services import measure as measure_module

    def _forbidden(geom, kind):
        raise AssertionError("measure() called without a measurement CRS")

    monkeypatch.setattr(measure_module, "measure", _forbidden)
    d = tmp_path / "infgeo"
    d.mkdir()
    _write_minimal_shapefile(
        d,
        "t",
        [[[(float("inf"), 0.0), (1e308, 0.0), (1e308, 1.0), (float("inf"), 0.0)]]],
        names=["odd"],
    )
    (d / "t.prj").write_text(
        CRS.from_epsg(4326).to_wkt("WKT1_GDAL"), encoding="utf-8"
    )  # WKT1: the .prj dialect the shapefile driver recognizes
    payload = _zip_bytes({
        "t.shp": (d / "t.shp").read_bytes(),
        "t.shx": (d / "t.shx").read_bytes(),
        "t.dbf": (d / "t.dbf").read_bytes(),
        "t.prj": (d / "t.prj").read_bytes(),
    })
    record = process_upload("t.zip", payload, db)
    assert record.status == "COMPLETED"
    assert record.source_crs == "EPSG:4326"
    assert record.measurement_crs is None
    assert record.feature_count == 1
    feat = db.query(Feature).filter_by(file_id=record.id).one()
    assert feat.status == "ERROR"
    assert feat.measure_type is None
    assert feat.measure_value is None
    assert feat.measure_unit is None
    assert feat.message and "bounds" in feat.message.lower()


def test_temp_data_cleaned(db, sample_kml_bytes, monkeypatch):
    seen: list[str] = []
    orig = tempfile.TemporaryDirectory

    def tracking(*args, **kwargs):
        handle = orig(*args, **kwargs)
        seen.append(handle.name)
        return handle

    monkeypatch.setattr(tempfile, "TemporaryDirectory", tracking)
    process_upload("sample.kml", sample_kml_bytes, db)
    assert seen, "expected TemporaryDirectory to be used"
    assert all(not Path(p).exists() for p in seen)

    seen.clear()
    process_upload("junk.kml", b"\x00\x01 garbage", db)
    assert seen and all(not Path(p).exists() for p in seen)

    seen.clear()
    with pytest.raises(InvalidArchiveError):
        process_upload("bad.zip", b"not a zip", db)
    assert seen and all(not Path(p).exists() for p in seen)


def test_restart_persistence(tmp_path, sample_kml_bytes):
    url = f"sqlite:///{tmp_path}/persist.db"
    engine = make_engine(url)
    init_db(engine)
    session = sessionmaker(bind=engine)()
    record = process_upload("sample.kml", sample_kml_bytes, session)
    file_id = record.id
    session.close()
    engine.dispose()

    engine2 = create_engine(url)
    init_db(engine2)  # create_all is idempotent; must not wipe data
    session2 = sessionmaker(bind=engine2)()
    again = session2.get(File, file_id)
    assert again is not None and again.status == "COMPLETED"
    assert again.feature_count == 3
    feats = session2.query(Feature).filter_by(file_id=file_id).all()
    assert len(feats) == 3
    assert {f.status for f in feats} == {"MEASURED", "NOT_APPLICABLE"}
    session2.close()
    engine2.dispose()
