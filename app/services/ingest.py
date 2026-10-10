"""Upload ingestion: validate, extract, read, reproject, measure, persist.

Pipeline: validate -> persist file -> extract/read -> resolve CRS ->
select measurement CRS -> prepare geometry -> transform -> measure ->
persist features -> finalize status.

Per-feature failures are isolated: one bad feature becomes ERROR while the
rest still process. Temporary extraction data lives in a
``TemporaryDirectory`` that is removed on every exit path.
"""

from __future__ import annotations

import io
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import shapely
from pyproj import CRS, Transformer
from shapely.ops import transform as shapely_transform
from sqlalchemy.orm import Session

from app import config
from app.models import Feature, File
from app.services import crs as crs_service
from app.services import geometry as geometry_service
from app.services import measure as measure_service
from app.services.readers import RawFeature, ReadError, read_kml, read_shapefile

_MIB = 1024 * 1024

_INPUT_EXTS = {".zip", ".kml"}


class UploadError(Exception):
    """Base class for upload failures mapped to HTTP error responses."""


class FileTooLargeError(UploadError):
    """Upload exceeds the configured size limit (HTTP 413)."""


class UnsupportedTypeError(UploadError):
    """Upload extension is not supported (HTTP 415)."""


class InvalidArchiveError(UploadError):
    """ZIP is unsafe, malformed, over limits, or misses sidecars (HTTP 422)."""


def _crs_label(crs: CRS) -> str:
    try:
        epsg = crs.to_epsg()
    except Exception:
        epsg = None
    if epsg is not None:
        return f"EPSG:{epsg}"
    try:
        return crs.to_string()
    except Exception:
        return "unknown"


def _safe_name(filename: str) -> str:
    name = Path(filename).name.strip() if filename else ""
    if not name:
        name = "upload"
    return name[:255]


# --- Safe ZIP extraction ---


def _reject(message: str) -> InvalidArchiveError:
    return InvalidArchiveError(message)


def safe_extract(data: bytes, dest: Path) -> Path:
    """Extract a shapefile ZIP into `dest`; return the resolved `.shp` path.

    Enforces member-count, declared-size, and actual decompressed-byte
    limits; rejects traversal, absolute paths, symlinks, and backslash
    separators; extracts only allowed sidecar extensions; requires exactly
    one `.shp` with matching `.shx`/`.dbf`. Partial output is removed on
    every error. Raises :class:`InvalidArchiveError` (never leaks `dest`).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def _clean() -> None:
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        _clean()
        raise _reject("malformed ZIP archive")

    max_members = config.MAX_ZIP_MEMBERS
    max_bytes = config.MAX_UNCOMPRESSED_MB * _MIB
    written: list[Path] = []
    try:
        with archive:
            try:
                infos = archive.infolist()
            except Exception:
                raise _reject("malformed ZIP archive")
            if len(infos) > max_members:
                raise _reject(
                    f"archive has {len(infos)} members, "
                    f"limit is {max_members}"
                )
            declared = sum(max(0, i.file_size) for i in infos)
            if declared > max_bytes:
                raise _reject(
                    "archive declared uncompressed size exceeds "
                    f"{config.MAX_UNCOMPRESSED_MB} MB"
                )
            try:
                dest_resolved = dest.resolve()
            except Exception:
                raise _reject("malformed ZIP archive")
            total_written = 0
            for info in infos:
                name = info.filename
                if not name:
                    continue
                # Validate every member path first, including directory
                # entries: a traversal-style directory is rejected even when
                # the archive also holds an otherwise valid shapefile.
                if (
                    name.startswith("/")
                    or name.startswith("\\")
                    or (len(name) > 1 and name[1] == ":")
                    or "\\" in name
                ):
                    raise _reject("archive contains an unsafe member path")
                if ".." in PurePosixPath(name).parts:
                    raise _reject("archive contains an unsafe member path")
                mode = (info.external_attr >> 16) & 0o170000
                if mode and stat.S_ISLNK(mode):
                    raise _reject("archive contains an unsupported symlink entry")
                if info.is_dir():
                    continue
                if Path(name).suffix.lower() not in config.ALLOWED_SHP_EXT:
                    continue  # counted member, ignored content
                target = dest / name
                try:
                    resolved = target.resolve()
                    resolved.relative_to(dest_resolved)
                except ValueError:
                    raise _reject("archive contains an unsafe member path")
                except Exception:
                    raise _reject("malformed ZIP archive")
                try:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as src, open(resolved, "wb") as out:
                        while True:
                            chunk = src.read(65536)
                            if not chunk:
                                break
                            total_written += len(chunk)
                            if total_written > max_bytes:
                                raise _reject(
                                    "archive decompressed size exceeds "
                                    f"{config.MAX_UNCOMPRESSED_MB} MB"
                                )
                            out.write(chunk)
                except InvalidArchiveError:
                    raise
                except Exception:
                    raise _reject("malformed ZIP archive")
                written.append(resolved)
            shps = [p for p in written if p.suffix.lower() == ".shp"]
            if len(shps) != 1:
                raise _reject("archive must contain exactly one .shp file")
            shp = shps[0]
            # Sidecars must share the .shp's relative directory and stem:
            # same basenames in different folders do not make a shapefile.
            shp_rel = shp.relative_to(dest_resolved).as_posix().lower()
            relatives = {
                p.relative_to(dest_resolved).as_posix().lower() for p in written
            }
            stem_rel = shp_rel[: -len(".shp")]
            if (
                f"{stem_rel}.shx" not in relatives
                or f"{stem_rel}.dbf" not in relatives
            ):
                raise _reject("archive is missing required .shx or .dbf sidecar")
            return shp
    except InvalidArchiveError:
        _clean()
        raise
    except Exception:
        _clean()
        raise _reject("malformed ZIP archive")


# --- Upload processing ---


def _fail(db: Session, record: File, message: str) -> File:
    record.status = "FAILED"
    record.error = message
    db.commit()
    return record


def _feature_bounds(features: list[RawFeature]) -> tuple | None:
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    any_usable = False
    for feature in features:
        geom = feature.geometry
        if geom is None or getattr(geom, "is_empty", False):
            continue
        try:
            b = geom.bounds
        except Exception:
            continue
        if b is None or len(b) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in b)
        except (TypeError, ValueError):
            continue
        import math

        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            continue
        if x0 > x1 or y0 > y1:
            continue
        any_usable = True
        minx, miny = min(minx, x0), min(miny, y0)
        maxx, maxy = max(maxx, x1), max(maxy, y1)
    if not any_usable:
        return None
    return (minx, miny, maxx, maxy)


def _persist_feature(
    db: Session,
    record: File,
    raw: RawFeature,
    transformer: Transformer | None,
) -> None:
    geom, repaired, prep_message = geometry_service.prepare(raw.geometry)
    if geom is None:
        geojson = None
        gtype = None
    else:
        try:
            geojson = shapely.geometry.mapping(geom)
        except Exception:
            geojson = None
        gtype = getattr(geom, "geom_type", None)
    if raw.geometry is not None and gtype is None:
        gtype = getattr(raw.geometry, "geom_type", None)

    if geom is None:
        if repaired:
            status, message = "ERROR", prep_message or "geometry could not be repaired"
        else:
            status, message = "EMPTY", prep_message or "empty geometry"
        db.add(
            Feature(
                file_id=record.id,
                idx=raw.index,
                geometry_type=gtype,
                geometry_geojson=None,
                properties=dict(raw.properties),
                geometry_repaired=bool(repaired),
                status=status,
                measure_type=None,
                measure_value=None,
                measure_unit=None,
                message=message,
            )
        )
        return

    kind = geometry_service.classify(geom)
    if kind == "unsupported":
        db.add(
            Feature(
                file_id=record.id,
                idx=raw.index,
                geometry_type=gtype,
                geometry_geojson=geojson,
                properties=dict(raw.properties),
                geometry_repaired=bool(repaired),
                status="UNSUPPORTED",
                measure_type=None,
                measure_value=None,
                measure_unit=None,
                message=f"unsupported geometry type: {gtype}",
            )
        )
        return
    if kind == "none":
        db.add(
            Feature(
                file_id=record.id,
                idx=raw.index,
                geometry_type=gtype,
                geometry_geojson=geojson,
                properties=dict(raw.properties),
                geometry_repaired=bool(repaired),
                status="NOT_APPLICABLE",
                measure_type=None,
                measure_value=None,
                measure_unit=None,
                message=None,
            )
        )
        return

    try:
        projected = (
            shapely_transform(transformer.transform, geom)
            if transformer is not None
            else geom
        )
        result = measure_service.measure(projected, kind)
    except Exception as exc:
        db.add(
            Feature(
                file_id=record.id,
                idx=raw.index,
                geometry_type=gtype,
                geometry_geojson=geojson,
                properties=dict(raw.properties),
                geometry_repaired=bool(repaired),
                status="ERROR",
                measure_type=None,
                measure_value=None,
                measure_unit=None,
                message=f"measurement failed: {exc}",
            )
        )
        return
    if result is None:
        db.add(
            Feature(
                file_id=record.id,
                idx=raw.index,
                geometry_type=gtype,
                geometry_geojson=geojson,
                properties=dict(raw.properties),
                geometry_repaired=bool(repaired),
                status="ERROR",
                measure_type=None,
                measure_value=None,
                measure_unit=None,
                message="measurement produced no value",
            )
        )
        return
    db.add(
        Feature(
            file_id=record.id,
            idx=raw.index,
            geometry_type=gtype,
            geometry_geojson=geojson,
            properties=dict(raw.properties),
            geometry_repaired=bool(repaired),
            status="MEASURED",
            measure_type=result["type"],
            measure_value=result["value"],
            measure_unit=result["unit"],
            message=None,
        )
    )


def _persist_unmeasurable(db: Session, record: File, raw: RawFeature) -> None:
    """Persist a non-empty feature that cannot be measured (ERROR).

    Used when no file-wide usable bounds exist, so no measurement CRS can
    be selected. The geometry is never passed to measure(): there are no
    value/unit to report. GeoJSON is left null because unusable bounds
    (e.g. non-finite coordinates) are not JSON-compatible.
    """
    try:
        gtype = (
            raw.geometry.geom_type if raw.geometry is not None else None
        )
    except Exception:
        gtype = None
    db.add(
        Feature(
            file_id=record.id,
            idx=raw.index,
            geometry_type=gtype,
            geometry_geojson=None,
            properties=dict(raw.properties),
            geometry_repaired=False,
            status="ERROR",
            measure_type=None,
            measure_value=None,
            measure_unit=None,
            message=(
                "geometry bounds are unusable; "
                "measurement CRS cannot be selected"
            ),
        )
    )


def process_upload(filename: str, data: bytes, db: Session) -> File:
    """Process one upload; return the persisted File record.

    Raises FileTooLargeError / UnsupportedTypeError / InvalidArchiveError
    for rejected uploads (no file record is left behind for invalid
    archives). Supported but unparsable uploads, or unresolvable CRS, are
    persisted as FAILED and returned (not raised).
    """
    ext = Path(filename).suffix.lower() if filename else ""
    if ext not in _INPUT_EXTS:
        raise UnsupportedTypeError(f"unsupported file type: {ext or '(none)'}")
    if len(data) > config.MAX_UPLOAD_MB * _MIB:
        raise FileTooLargeError(
            f"upload exceeds {config.MAX_UPLOAD_MB} MB"
        )

    safe_name = _safe_name(filename)
    with tempfile.TemporaryDirectory(prefix="geo_measure_") as tmp:
        tmpdir = Path(tmp)
        if ext == ".zip":
            # Archive safety is validated before any persistent record exists.
            shp_path = safe_extract(data, tmpdir / "extract")
            record = File(
                id=uuid.uuid4().hex,
                filename=safe_name,
                status="PROCESSING",
            )
            db.add(record)
            db.commit()
            try:
                result = read_shapefile(shp_path)
            except ReadError as exc:
                return _fail(db, record, str(exc))
            except Exception:
                return _fail(db, record, "cannot parse shapefile contents")
        else:
            kml_path = tmpdir / "upload.kml"
            try:
                kml_path.write_bytes(data)
            except Exception:
                raise InvalidArchiveError("cannot stage upload")
            record = File(
                id=uuid.uuid4().hex,
                filename=safe_name,
                status="PROCESSING",
            )
            db.add(record)
            db.commit()
            try:
                result = read_kml(kml_path)
            except ReadError as exc:
                return _fail(db, record, str(exc))
            except Exception:
                return _fail(db, record, "cannot parse KML contents")

        try:
            source_crs, assumed = crs_service.resolve_source_crs(result)
        except ValueError as exc:
            return _fail(db, record, str(exc))
        except Exception:
            return _fail(db, record, "source CRS cannot be resolved")
        record.source_crs = _crs_label(source_crs)
        record.crs_assumed = bool(assumed)

        bounds = _feature_bounds(result.features)
        if bounds is None:
            # Readable input with a resolvable CRS but no usable bounds:
            # no measurement CRS can be selected, so nothing may be
            # measured. A truly empty dataset completes with zero features
            # and a null measurement CRS rather than an invented UTM zone.
            # Missing/empty geometries stay EMPTY; any non-empty geometry
            # is a feature-level ERROR (never measured without a CRS).
            if not result.features:
                record.measurement_crs = None
                record.warnings = []
                record.feature_count = 0
                record.status = "COMPLETED"
                record.error = None
                db.commit()
                return record
            for raw in result.features:
                if raw.geometry is None or getattr(
                    raw.geometry, "is_empty", False
                ):
                    _persist_feature(db, record, raw, None)
                else:
                    _persist_unmeasurable(db, record, raw)
            record.measurement_crs = None
            record.warnings = []
            record.feature_count = len(result.features)
            record.status = "COMPLETED"
            record.error = None
            db.commit()
            return record

        try:
            measurement_crs, warnings = crs_service.select_measurement_crs(
                source_crs, bounds
            )
        except ValueError as exc:
            return _fail(db, record, str(exc))
        except Exception:
            return _fail(db, record, "measurement CRS cannot be selected")
        record.measurement_crs = _crs_label(measurement_crs)
        record.warnings = list(warnings)

        try:
            if measurement_crs == source_crs:
                transformer = None
            else:
                transformer = Transformer.from_crs(
                    source_crs, measurement_crs, always_xy=True
                )
        except Exception:
            return _fail(db, record, "measurement CRS cannot be selected")

        try:
            for raw in result.features:
                try:
                    _persist_feature(db, record, raw, transformer)
                except Exception as exc:
                    db.add(
                        Feature(
                            file_id=record.id,
                            idx=raw.index,
                            geometry_type=None,
                            geometry_geojson=None,
                            properties={},
                            geometry_repaired=False,
                            status="ERROR",
                            measure_type=None,
                            measure_value=None,
                            measure_unit=None,
                            message=f"measurement failed: {exc}",
                        )
                    )
            record.feature_count = len(result.features)
            record.status = "COMPLETED"
            record.error = None
            db.commit()
        except Exception:
            db.rollback()
            return _fail(db, record, "processing failed")
        return record
