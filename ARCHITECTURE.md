# ARCHITECTURE.md — geo-measure-api

This document is the source of truth for API routes, schemas, and module
responsibilities.

## 1. Overview

`geo-measure-api` is a small FastAPI REST service that:

- accepts ZIP archives containing ESRI Shapefiles and standalone KML files,
- reads geographic features and resolves their source CRS,
- reprojects to a per-file measurement CRS (retained projected-metre CRS or
  selected UTM / EPSG:6933 polar fallback),
- computes area (`m2`) or length (`m`) after reprojection,
- persists files + features in SQLite,
- exposes three REST endpoints with deterministic JSON contracts.

Out of scope for this MVP: authentication, PostGIS, background queues,
and a UI.

## 2. API contract

Base: FastAPI app `app.main:app`. Swagger UI at `/docs`.

### 2.1 `POST /api/files/`

- Request: `multipart/form-data` with optional file field named `file`
  (`filename`, bytes). Declared optional at the FastAPI boundary so a missing
  file returns `400 NO_FILE` instead of FastAPI's default 422.
- Uploads are re-read from the received `UploadFile` in bounded 1 MiB
  chunks; files over 25 MB return `413 FILE_TOO_LARGE` while reading,
  before extension checks. Starlette parses/spools the multipart body
  before the route runs, so this limits materialization, not body parsing
  (see README for proxy-level recommendations).
- Unsupported extension (not `.zip` / `.kml`) returns `415 UNSUPPORTED_TYPE`.
- Unsafe/malformed ZIP, traversal, archive-limit violations, or missing
  required shapefile sidecars return `422 INVALID_ARCHIVE` with no persisted
  file record.
- A structurally acceptable supported upload whose contents cannot be parsed
  or whose source CRS cannot be resolved returns HTTP `201` with a persisted
  `FileInfo` having `status = FAILED` and an `error` message.
- Otherwise returns HTTP `201` with `FileInfo` (`COMPLETED` normally).

### 2.2 `GET /api/files/{id}/`

- Returns `FileInfo` for the persisted file.
- Unknown ID returns `404 NOT_FOUND` with `ErrorOut`.

### 2.3 `GET /api/files/{id}/measurements/`

- Returns `MeasurementsOut`: `{ file_id, measurements: FeatureOut[] }`.
- Unknown ID returns `404 NOT_FOUND`.

### 2.4 Schemas

**FileInfo** (exact fields):

```
id, filename, status, error, source_crs, measurement_crs, crs,
crs_assumed, feature_count, warnings, created_at
```

- `status`: `PROCESSING | COMPLETED | FAILED`
- `source_crs` / `measurement_crs`: EPSG strings such as `EPSG:4326` or
  `EPSG:32633`, or `null` when unresolvable/inapplicable (empty dataset).
- `crs`: equals `source_crs`, including `null` when the source CRS is
  unavailable. Derived at response time, not stored.
- `warnings`: `string[]`.
- `created_at`: ISO-8601 datetime.

**FeatureOut** (exact fields):

```
id, file_id, idx, geometry_type, geometry_geojson, properties,
geometry_repaired, status, measure_type, measure_value, measure_unit, message,
crs
```

- `status`: `MEASURED | NOT_APPLICABLE | UNSUPPORTED | EMPTY | ERROR`
- `measure_type`: `area | length | null`; `measure_unit`: `m2 | m | null`
  (null when no measurement applies).
- `crs`: the owning file's source CRS, never the measurement CRS; `null`
  when the source CRS is unavailable. Derived from the File record, not stored.
- `geometry_geojson`: JSON-compatible GeoJSON dict or `null`.
- `properties`: JSON-compatible dict.

**MeasurementsOut**: `{ file_id, measurements: FeatureOut[] }`.

**ErrorOut**: `{ detail: str, code: str }`.

### 2.5 Error mapping (exact)

| Situation | HTTP | Code/behavior |
|---|---|---|
| Missing upload | 400 | `NO_FILE` |
| Upload over 25 MB | 413 | `FILE_TOO_LARGE` |
| Unsupported extension | 415 | `UNSUPPORTED_TYPE` |
| Unsafe/malformed ZIP, traversal, archive limits exceeded, missing required sidecars | 422 | `INVALID_ARCHIVE`, no file record left behind |
| Unknown file ID | 404 | `NOT_FOUND` |
| Supported upload, unparsable or CRS unresolvable | 201 | persisted `status = FAILED` + error message |
| Other request-validation failures | 422 | `VALIDATION_ERROR` |
| Unexpected server error | 500 | generic body, no traceback/paths |

Never return `INVALID_GEOSPATIAL_FILE`.

## 3. Modules

- `app/config.py`: limits (`MAX_UPLOAD_MB=25`, `MAX_ZIP_MEMBERS=50`,
  `MAX_UNCOMPRESSED_MB=200`, `ALLOWED_SHP_EXT`), `DATABASE_URL` env handling,
  project-root-anchored default SQLite path.
- `app/api/`: HTTP routes + error mapping. No CRS/geometry math.
- `app/services/readers.py`: `RawFeature`, `ReadResult`, `read_shapefile`,
  `read_kml` (pyogrio first, lxml fallback only if needed). No network XML.
- `app/services/crs.py`: `resolve_source_crs`, `select_measurement_crs`.
  All pyproj transformers use `always_xy=True`.
- `app/services/geometry.py`: `prepare`, `classify` → `area|length|none|
  unsupported`; `make_valid` repair path.
- `app/services/measure.py`: `measure(geom_projected, kind)` → area/length
  dicts or `None`; finite built-in floats only.
- `app/services/ingest.py`: `safe_extract`, `process_upload`; validate →
  persist → extract/read → resolve CRS → select CRS → prepare → classify →
  transform → measure → persist → finalize; per-feature isolation; temp cleanup.
- `app/db.py`: engine/session/init, FK enforcement, test isolation.
- `app/models.py`: `files` + `features` tables per prompt §4.
- `app/schemas.py`: Pydantic `FileInfo`, `FeatureOut`, `MeasurementsOut`,
  `ErrorOut`.
- `tests/`: `test_measure.py`, `test_crs.py`, `test_ingest.py`,
  `test_api.py`.
- `data/`: `sample.kml`, `sample-shapefile.zip` (small, deterministic).
- `scripts/generate_sample_data.py`: deterministic generator.
- `docs/NOTES.md`: plain-language per-module notes.

## 4. CRS decisions (locked)

- KML → `EPSG:4326`.
- Shapefile → `.prj` CRS when present.
- Missing `.prj` → assume `EPSG:4326` (`crs_assumed=True`) only if every
  usable geometry bound is plausible lon/lat (`[-180,180] x [-90,90]`,
  finite); otherwise reject (→ persisted `FAILED`).
- Retain projected source CRS whose linear units are metres, except
  non-transverse Mercator methods — EPSG:3857, EPSG:3785, EPSG:900913,
  EPSG:3395 and the like, detected via the operation method name with the
  explicit EPSG list as fallback. Unretained CRSs go through normal
  selection: UTM within its latitude range, EPSG:6933 outside it.
- Else select UTM from geographic bbox centre: zone
  `clamp(floor((lon+180)/6)+1, 1, 60)`; `326xx` north / `327xx` south.
- Centre lat outside `[-80, +84]` (inclusive boundaries valid) → `EPSG:6933`
  + warning. A dataset may extend beyond the range while its centre stays
  inside (documented limitation).
- Lon span > 6° → warning (implemented; advanced multi-zone handling is
  out of scope).
- Projected bounds are converted to geographic via
  `Transformer(source → 4326, always_xy=True)` before UTM selection /
  validation. Raw eastings/northings are never treated as lon/lat.
- NaN/inf/empty/inverted bounds are rejected with `ValueError`.

## 5. Persistence & lifecycle

`validate → persist file → extract/read → resolve CRS → select measurement
CRS → prepare → classify → transform → measure → persist features → finalize`.

- File: `PROCESSING → COMPLETED | FAILED`.
- Feature: `MEASURED | NOT_APPLICABLE (points) | UNSUPPORTED |
  EMPTY | ERROR (per-feature, processing continues)`.
- `INVALID_ARCHIVE` leaves no file/feature rows (validate before record
  where practical, else rollback/delete; partial extraction removed).
- Zero-feature readable dataset with resolvable CRS → `COMPLETED`,
  `feature_count=0`, null measurement CRS (no invented UTM).
- Restart against same SQLite file must preserve completed data.

## 6. Security notes

- Treat filenames, bytes, ZIP members, geometries, properties, parser errors
  as untrusted.
- Enforce 25 MB upload (bounded re-reads of the received file; a
  proxy-level body limit is still recommended), 50 members, 200 MB declared
  + actual decompressed limits.
- Reject traversal/absolute paths/symlinks/unsupported entries; resolve each
  target inside the extraction dir; require exactly one `.shp` + `.shx` +
  `.dbf`; `.prj`/`.cpg` optional.
- Parameterized ORM access; FK on; generic 500s without paths/tracebacks.
- The async upload route runs blocking GDAL/Shapely/SQLite work inline,
  which blocks the event loop under load — local/demo use only.
- No auth: file IDs are not authorization; local/demo use only.
