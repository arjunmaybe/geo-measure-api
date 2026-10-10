"""API integration tests (Phase 3).

Uses isolated temporary file-backed SQLite databases via
dependency_overrides. Trailing-slash routes are exercised exactly, without
following redirects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import get_db, init_db, make_engine
from app.main import app
from app.models import Feature, File
from tests.test_ingest import _write_minimal_shapefile, _zip_bytes

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FILE_INFO_FIELDS = {
    "id",
    "filename",
    "status",
    "error",
    "source_crs",
    "measurement_crs",
    "crs",
    "crs_assumed",
    "feature_count",
    "warnings",
    "created_at",
}

FEATURE_OUT_FIELDS = {
    "id",
    "file_id",
    "idx",
    "geometry_type",
    "geometry_geojson",
    "properties",
    "geometry_repaired",
    "status",
    "measure_type",
    "measure_value",
    "measure_unit",
    "message",
    "crs",
}


def _build_client(url: str, raise_server_exceptions: bool = True):
    engine = make_engine(url)
    init_db(engine)
    test_sessions = sessionmaker(bind=engine)

    def override():
        session = test_sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    return client, engine, test_sessions


@pytest.fixture()
def env(tmp_path):
    client, engine, sessions = _build_client(f"sqlite:///{tmp_path}/api.db")
    yield {"client": client, "sessions": sessions, "engine": engine}
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def sample_zip_bytes():
    return (DATA_DIR / "sample-shapefile.zip").read_bytes()


@pytest.fixture()
def sample_kml_bytes():
    return (DATA_DIR / "sample.kml").read_bytes()


def _db_file_count(sessions) -> int:
    session = sessions()
    try:
        return session.query(File).count()
    finally:
        session.close()


def test_post_zip_upload(env, sample_zip_bytes):
    client = env["client"]
    resp = client.post(
        "/api/files/",
        files={"file": ("sample-shapefile.zip", sample_zip_bytes)},
        follow_redirects=False,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body.keys()) == FILE_INFO_FIELDS
    assert body["status"] == "COMPLETED"
    assert body["filename"] == "sample-shapefile.zip"
    assert body["source_crs"] == "EPSG:4326"
    assert body["crs"] == body["source_crs"]
    assert body["measurement_crs"] == "EPSG:32632"
    assert body["crs_assumed"] is False
    assert body["feature_count"] == 2
    assert body["error"] is None


def test_post_kml_upload(env, sample_kml_bytes):
    client = env["client"]
    resp = client.post(
        "/api/files/",
        files={"file": ("sample.kml", sample_kml_bytes)},
        follow_redirects=False,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["crs"] == body["source_crs"] == "EPSG:4326"
    assert body["feature_count"] == 3


def test_get_file_info(env, sample_kml_bytes):
    client = env["client"]
    file_id = client.post(
        "/api/files/",
        files={"file": ("sample.kml", sample_kml_bytes)},
    ).json()["id"]
    resp = client.get(f"/api/files/{file_id}/", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    assert resp.status_code not in (307, 308)
    body = resp.json()
    assert set(body.keys()) == FILE_INFO_FIELDS
    assert body["id"] == file_id
    assert body["status"] == "COMPLETED"


def test_get_measurements(env, sample_kml_bytes):
    client = env["client"]
    file_id = client.post(
        "/api/files/",
        files={"file": ("sample.kml", sample_kml_bytes)},
    ).json()["id"]
    resp = client.get(
        f"/api/files/{file_id}/measurements/", follow_redirects=False
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"file_id", "measurements"}
    assert body["file_id"] == file_id
    assert len(body["measurements"]) == 3
    for feat in body["measurements"]:
        assert set(feat.keys()) == FEATURE_OUT_FIELDS
        # Per-feature crs is the owning file's *source* CRS, never the
        # measurement CRS.
        assert feat["crs"] == "EPSG:4326"
    statuses = [f["status"] for f in body["measurements"]]
    assert "MEASURED" in statuses
    # Point feature: area/length does not apply.
    points = [f for f in body["measurements"] if f["geometry_type"] == "Point"]
    assert len(points) == 1
    assert points[0]["status"] == "NOT_APPLICABLE"
    assert points[0]["measure_type"] is None
    assert points[0]["measure_value"] is None


def test_unknown_id_404(env):
    client = env["client"]
    for path in ("/api/files/does-not-exist/", "/api/files/does-not-exist/measurements/"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "File not found", "code": "NOT_FOUND"}


def test_missing_upload_400(env):
    client = env["client"]
    resp = client.post("/api/files/", follow_redirects=False)
    assert resp.status_code == 400
    assert resp.json()["code"] == "NO_FILE"


def test_oversized_upload_413(env):
    client = env["client"]
    big = b"x" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    resp = client.post(
        "/api/files/",
        files={"file": ("big.zip", big)},
        follow_redirects=False,
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "FILE_TOO_LARGE"
    assert _db_file_count(env["sessions"]) == 0


def test_oversized_wrong_extension_still_413(env):
    # Size is enforced during chunked reading, before extension checks.
    client = env["client"]
    big = b"x" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    resp = client.post(
        "/api/files/",
        files={"file": ("big.txt", big)},
        follow_redirects=False,
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "FILE_TOO_LARGE"
    assert _db_file_count(env["sessions"]) == 0


def test_unsupported_extension_415(env):
    client = env["client"]
    resp = client.post(
        "/api/files/",
        files={"file": ("notes.txt", b"hello")},
        follow_redirects=False,
    )
    assert resp.status_code == 415
    assert resp.json()["code"] == "UNSUPPORTED_TYPE"
    assert _db_file_count(env["sessions"]) == 0


def test_invalid_archive_422_no_record(env):
    client = env["client"]
    payload = _zip_bytes({"../../evil.shp": b"evil"})
    resp = client.post(
        "/api/files/",
        files={"file": ("evil.zip", payload)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_ARCHIVE"
    assert _db_file_count(env["sessions"]) == 0


def test_unreadable_supported_returns_201_failed(env):
    client = env["client"]
    resp = client.post(
        "/api/files/",
        files={"file": ("junk.kml", b"\x00\x01\x02 binary garbage")},
        follow_redirects=False,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"]
    assert body["source_crs"] is None
    assert body["crs"] is None
    assert _db_file_count(env["sessions"]) == 1


def test_failed_upload_full_response_body(env):
    # A FAILED upload returns the complete FileInfo contract, not a subset.
    resp = env["client"].post(
        "/api/files/",
        files={"file": ("junk.kml", b"\x00\x01\x02 binary garbage")},
        follow_redirects=False,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body.keys()) == FILE_INFO_FIELDS
    assert body["filename"] == "junk.kml"
    assert body["status"] == "FAILED"
    assert isinstance(body["error"], str) and body["error"]
    assert body["source_crs"] is None
    assert body["measurement_crs"] is None
    assert body["crs"] is None
    assert body["crs_assumed"] is False
    assert body["feature_count"] == 0
    assert body["warnings"] == []
    assert isinstance(body["id"], str) and body["id"]
    from datetime import datetime

    assert datetime.fromisoformat(body["created_at"])


def test_unresolved_crs_returns_201_failed(env, tmp_path):
    # Test-only fixture: projected coordinates without .prj (kept out of data/).
    d = tmp_path / "noprj"
    d.mkdir()
    _write_minimal_shapefile(
        d,
        "projected",
        [[[(500000.0, 0.0), (500100.0, 0.0), (500100.0, 100.0),
           (500000.0, 100.0), (500000.0, 0.0)]]],
        names=["block"],
    )
    payload = _zip_bytes({
        "projected.shp": (d / "projected.shp").read_bytes(),
        "projected.shx": (d / "projected.shx").read_bytes(),
        "projected.dbf": (d / "projected.dbf").read_bytes(),
    })
    client = env["client"]
    resp = client.post(
        "/api/files/",
        files={"file": ("projected.zip", payload)},
        follow_redirects=False,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"]
    assert body["crs"] == body["source_crs"]
    assert _db_file_count(env["sessions"]) == 1


def test_unexpected_error_500_without_leak(env, tmp_path, sample_kml_bytes, monkeypatch):
    from app.services import ingest as ingest_module

    def _boom(filename, data, db):
        raise RuntimeError("simulated service boom")

    monkeypatch.setattr(ingest_module, "process_upload", _boom)
    url = f"sqlite:///{tmp_path}/api500.db"
    client, engine, _ = _build_client(url, raise_server_exceptions=False)
    try:
        resp = client.post(
            "/api/files/",
            files={"file": ("sample.kml", sample_kml_bytes)},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
    assert resp.status_code == 500
    body = resp.json()
    assert set(body.keys()) == {"detail", "code"}
    assert "Traceback" not in resp.text
    assert "boom" not in resp.text


def test_feature_crs_nullable_when_source_unavailable(env):
    # No ingest path persists features without a source CRS, so insert
    # directly: serialization must still emit crs as null on both shapes.
    session = env["sessions"]()
    try:
        session.add(
            File(
                id="noloc",
                filename="x.kml",
                status="COMPLETED",
                source_crs=None,
                measurement_crs=None,
                crs_assumed=False,
                feature_count=1,
                warnings=[],
            )
        )
        session.add(
            Feature(
                file_id="noloc",
                idx=0,
                status="EMPTY",
                properties={},
                geometry_repaired=False,
            )
        )
        session.commit()
    finally:
        session.close()
    client = env["client"]
    meas = client.get("/api/files/noloc/measurements/", follow_redirects=False)
    assert meas.status_code == 200
    assert meas.json()["measurements"][0]["crs"] is None
    info = client.get("/api/files/noloc/", follow_redirects=False).json()
    assert info["source_crs"] is None
    assert info["crs"] is None


def test_restart_persistence(tmp_path, sample_kml_bytes):
    url = f"sqlite:///{tmp_path}/restart.db"
    client1, engine1, _ = _build_client(url)
    file_id = client1.post(
        "/api/files/",
        files={"file": ("sample.kml", sample_kml_bytes)},
    ).json()["id"]
    app.dependency_overrides.clear()
    engine1.dispose()

    client2, engine2, _ = _build_client(url)
    try:
        info = client2.get(f"/api/files/{file_id}/", follow_redirects=False)
        assert info.status_code == 200
        assert info.json()["status"] == "COMPLETED"
        meas = client2.get(
            f"/api/files/{file_id}/measurements/", follow_redirects=False
        )
        assert meas.status_code == 200
        assert len(meas.json()["measurements"]) == 3
    finally:
        app.dependency_overrides.clear()
        engine2.dispose()
