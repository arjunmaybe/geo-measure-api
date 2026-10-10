# geo-measure-api

## Overview

`geo-measure-api` is a small local/demo REST API that ingests geospatial files,
measures their features, and stores the results in SQLite.

Supported inputs:

- ZIP archives (`.zip`) containing exactly one ESRI Shapefile (`.shp` with
  matching `.shx` and `.dbf`; `.prj` and `.cpg` optional).
- Standalone KML files (`.kml`) with Point, LineString, and Polygon placemarks
  (polygon holes supported).

For each upload the API reads the features, resolves the source coordinate
reference system (CRS), reprojects to one per-file measurement CRS, computes
area in `m2` (polygons) or length in `m` (linestrings), and persists the file
plus per-feature results. Points report `NOT_APPLICABLE`; empty geometries
report `EMPTY`; unsupported types report `UNSUPPORTED`; per-feature failures
report `ERROR` without aborting the rest of the file.

Out of scope for this MVP: authentication, PostGIS, background queues,
and a UI. There is **no
authentication and no per-user isolation**: file IDs are random but are not
an authorization mechanism. This API is for local development and
demonstration only — do not expose it to untrusted public traffic without a
deliberate access-control policy and request-rate/concurrency limits.

## Architecture

Module boundaries (see `ARCHITECTURE.md`, the source of truth for routes,
schemas, and CRS decisions):

- `app/api/` — HTTP routes (`files.py`) and error mapping (`errors.py`).
  No CRS or geometry math lives here.
- `app/services/readers.py` — `read_shapefile` / `read_kml` returning
  `RawFeature` / `ReadResult`. Reads use `pyogrio.raw` (no GeoPandas at
  runtime); KML has a secure lxml fallback (no network, no entities).
- `app/services/crs.py` — source-CRS resolution and per-file measurement-CRS
  selection (UTM or EPSG:6933 polar fallback). Every transformer uses
  `always_xy=True`.
- `app/services/geometry.py` — `prepare` (incl. `make_valid` repair) and
  `classify` (`area` / `length` / `none` / `unsupported`).
- `app/services/measure.py` — measures already-projected geometries only.
- `app/services/ingest.py` — `safe_extract` + `process_upload`: validate →
  persist file → extract/read → resolve CRS → select measurement CRS →
  prepare → classify → transform → measure → persist features → finalize.
- `app/db.py` / `app/models.py` — SQLite engine/sessions and the `files` +
  `features` tables (foreign keys enforced).
- `app/schemas.py` — Pydantic response contracts.

Ingestion isolates failures per feature: one bad geometry becomes `ERROR`
with a message while valid siblings still measure. Temporary extraction
directories are removed on every exit path. An unsafe/malformed archive
(`INVALID_ARCHIVE`) leaves no file or feature rows behind; a supported but
unparsable upload (or unresolvable CRS) persists a `FAILED` file record.

## Setup and Run

Requirements: Python 3.10+ (tested on 3.10.11), pip. Runtime dependencies
are pinned in `requirements.txt` (FastAPI, Uvicorn, SQLAlchemy, Shapely,
pyproj, pyogrio, lxml, httpx, pytest, …). GeoPandas is **not** a runtime
dependency; it is only needed to regenerate the sample shapefile (see
`scripts/generate_sample_data.py`).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
python -m pytest -q
```

If PowerShell blocks activation, run the following in that PowerShell process
and retry activation (process scope only, not a machine-wide change):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

The default SQLite database lives at the project root:
`<project-root>/geo_measure.db` (anchored to the project root, not the
working directory). Override it with the `DATABASE_URL` environment variable,
e.g. `sqlite:///C:/data/geo.db`. Swagger UI is served at `/docs` once the
app runs. Manual backup: stop the app and copy the `.db` file; restore by
copying it back. There is no automated backup system.

## API Usage

All three routes use exact trailing slashes.

### `POST /api/files/` — upload (201)

`multipart/form-data` with a `file` field (`.zip` or `.kml`, max 25 MB,
read in bounded 1 MiB chunks). Validation precedence: missing upload → 400;
oversized (detected while streaming, before anything else) → 413;
unsupported extension → 415; unsafe/malformed ZIP or missing sidecars → 422
with no record created. Supported-but-unparsable content (or unresolvable
CRS) returns **201** with a persisted `FAILED` file record.

Upload-size note: the 25 MB check runs while the route reads the received
`UploadFile` in bounded chunks, but the multipart body itself is first
parsed/spooled by FastAPI/Starlette (python-multipart) before the route
runs — so this is application-level protection, not end-to-end network
streaming protection. Before any public deployment, also configure a
request-body limit at the server or reverse proxy (e.g. the proxy's max
body size), alongside the access-control and rate-limiting measures noted
in Overview.

```powershell
curl.exe -F "file=@data/sample.kml" http://127.0.0.1:8000/api/files/
curl.exe -F "file=@data/sample-shapefile.zip" http://127.0.0.1:8000/api/files/
```

Example response (KML sample, values measured after reprojection to UTM 32N):

```json
{
  "id": "dfb0bcd9452648d3b8c724f06b357bb4",
  "filename": "sample.kml",
  "status": "COMPLETED",
  "error": null,
  "source_crs": "EPSG:4326",
  "measurement_crs": "EPSG:32632",
  "crs": "EPSG:4326",
  "crs_assumed": false,
  "feature_count": 3,
  "warnings": [],
  "created_at": "2026-10-10T02:42:37.690424"
}
```

### `GET /api/files/{id}/` — file info (200)

```powershell
curl.exe http://127.0.0.1:8000/api/files/dfb0bcd9452648d3b8c724f06b357bb4/
```

Returns the same `FileInfo` shape as above. Unknown ID → 404:

```json
{"detail": "File not found", "code": "NOT_FOUND"}
```

### `GET /api/files/{id}/measurements/` — measurements (200)

```powershell
curl.exe http://127.0.0.1:8000/api/files/dfb0bcd9452648d3b8c724f06b357bb4/measurements/
```

Returns `{"file_id": "…", "measurements": [FeatureOut, …]}` ordered by `idx`.
Example entries (trimmed):

```json
{"idx": 0, "geometry_type": "Polygon", "status": "MEASURED", "crs": "EPSG:4326",
 "measure_type": "area", "measure_value": 7650.429939420929, "measure_unit": "m2"}
{"idx": 1, "geometry_type": "LineString", "status": "MEASURED", "crs": "EPSG:4326",
 "measure_type": "length", "measure_value": 143.34321383264455, "measure_unit": "m"}
{"idx": 2, "geometry_type": "Point", "status": "NOT_APPLICABLE", "crs": "EPSG:4326",
 "measure_type": null, "measure_value": null, "measure_unit": null}
```

Each feature's `crs` is the owning file's source CRS (never the measurement
CRS); `FileInfo.crs` mirrors `source_crs`. Both are `null` when the source
CRS is unavailable.

### Error mapping

| Situation | HTTP | Code |
|---|---|---|
| Missing upload | 400 | `NO_FILE` |
| Upload over 25 MB | 413 | `FILE_TOO_LARGE` |
| Unsupported extension | 415 | `UNSUPPORTED_TYPE` |
| Unsafe/malformed ZIP, traversal, limit violations, missing sidecars | 422 | `INVALID_ARCHIVE` (no record left) |
| Unknown file ID | 404 | `NOT_FOUND` |
| Supported but unparsable / CRS unresolvable | 201 | persisted `FAILED` + error message |
| Other validation failures | 422 | `VALIDATION_ERROR` |
| Unexpected server error | 500 | `INTERNAL_ERROR` (generic, no traceback/paths) |

All error bodies share the flat shape `{"detail": "…", "code": "…"}`.

## Coordinate Reference Systems and Measurements

- KML defaults to `EPSG:4326`. Shapefiles use their `.prj` CRS when present
  (write it as WKT1 — e.g. `WKT1_GDAL` — since that is the dialect the
  shapefile driver recognizes).
- A shapefile without `.prj` is assumed to be `EPSG:4326`
  (`crs_assumed: true`) only if every usable geometry bound is plausible
  longitude/latitude; otherwise the upload persists as `FAILED`.
- A projected source CRS with metre units is kept as the measurement CRS,
  except non-transverse Mercator methods (Web-Mercator auxiliaries
  EPSG:3857, EPSG:3785, EPSG:900913 and World Mercator EPSG:3395, detected
  via the operation method name with that EPSG list as fallback): their
  metres distort area badly away from the equator, so they are not retained
  and go through normal selection (UTM within its latitude range, EPSG:6933
  outside it; regression tested near 45°N against a geodesic reference
  within 1%).
  Otherwise one UTM zone is selected from the geographic bounding-box centre
  (`326xx` north / `327xx` south, zone clamped to 1–60).
- A centre latitude outside −80°…+84° (inclusive) selects `EPSG:6933` with a
  warning. Note this depends on the **bounding-box centre**: a dataset can
  extend beyond the UTM latitude range while its centre stays inside, in
  which case UTM is still selected — a documented limitation.
- A geographic longitude span over 6° adds a warning (single-zone
  distortion). Projected bounds are always converted to geographic
  coordinates before zone selection; raw eastings/northings are never read
  as longitude/latitude. All transformers use `always_xy=True`.
- Measurements happen only after reprojection, in metres (`m2`, `m`),
  returned as finite built-in floats. NaN/infinite/empty/inverted bounds are
  rejected, never silently assigned.
- Known limitation: repairing an invalid geometry with `make_valid()` can
  return a mixed `GeometryCollection`; the classifier marks it
  `unsupported`, so polygonal area inside that collection may not be
  measured even though the repair itself succeeded.

## Design Decisions

- **UTM projection vs geodesic math.** One per-file UTM CRS keeps the code
  small and the units plainly metres; per-feature geodesic math would be
  more uniformly accurate but kept out of scope for this MVP.
  UTM error is small near the central meridian (the suite checks projected
  area against an independent `pyproj.Geod` reference within 1%, a bound
  chosen to catch degree-vs-metre mistakes, not to certify survey accuracy).
  Alternatives were not benchmarked.
- **SQLite vs PostGIS.** SQLite needs no server and matches the MVP's
  single-file persistence and restart requirements; PostGIS would add
  deployment weight for no required spatial-query feature.
- **Synchronous processing vs a queue.** Uploads process inline so results
  are immediate and the code stays linear; sustained load or large files
  would need a background queue plus progress polling (not implemented).
  Note the upload route is `async` but calls blocking GDAL/Shapely/SQLite
  work inline, which blocks the event loop — another reason this API is
  local/demo only.
- **One measurement CRS per file vs per feature.** One CRS per file keeps
  measurements within a file comparable and the schema simple; wide or
  multi-zone datasets accept single-zone distortion (flagged by warning).

## Learnings

- Axis order is the quietest geospatial bug source: `always_xy=True` on
  every transformer plus an asymmetric regression test (lon −75.1/lat 40 →
  32618, swapped → 32737) locks it in.
- `pyogrio.raw.read` covers shapefile and KML reading with only NumPy,
  keeping GeoPandas out of the runtime; the KML driver's field names
  (capital `Name`) still leak into properties, so contracts must tolerate
  driver-specific keys.
- If GDAL cannot resolve the CRS from a `.prj`, CRS resolution may fail.
  If bounds are not plausible longitude/latitude, the app does not guess
  EPSG:4326 and may persist the upload as `FAILED`.
- Central-directory metadata can lie, so archive limits must be enforced on
  both declared sizes and streamed bytes; the suite simulates a
  decompression bomb to prove the streaming counter aborts mid-extraction.
- Measuring before establishing a valid measurement CRS is a real defect
  class (found in review): the no-bounds branch now records ERROR instead
  of measuring raw degrees.

## Future Scope

- Wrap-aware UTM selection for narrow antimeridian-crossing datasets.
- Per-feature geodesic measurements alongside projected ones.
- Duplicate/conflicting archive-member detection beyond current safeguards.
- Optional ZIP magic-byte check (documented limitations, tested).
- Background processing with progress endpoints for large uploads.
- Authentication and per-user isolation before any shared deployment.
- Additional KML geometry coverage (MultiGeometry as first-class measured
  types where meaningful).

## Tests

```powershell
python -m pytest -q
```

95 tests across `test_crs.py` (UTM/polar/bounds/axis-order, Web-Mercator
exclusion), `test_measure.py` (analytical area/length, geodesic agreement,
non-finite rejection), `test_geometry.py` (prepare/classify repair
contract), `test_ingest.py` (traversal, member/size/decompressed limits,
sidecars, KML folders incl. same-name folders, 45N Web-Mercator-vs-geodesic,
FAILED persistence, isolation, zero-feature, restart), and `test_api.py` (uploads, retrieval,
all error codes, full FAILED body, trailing slashes, generic 500,
restart). Sample data is generated by
`python scripts/generate_sample_data.py` (needs GeoPandas, sample-use only)
and verified with `python scripts/verify_samples.py`, which reads the KML
directly and the ZIP through the app's own `safe_extract`.

Omissions: no lint/type tool is configured (only `compileall` sanity);
performance, concurrency, and production readiness were not measured or
claimed. Manual verification performed after pushing `e00a1ae`: cloned that
exact commit into a separate directory, installed requirements with the
isolated `publish-check-venv`, ran all 95 tests successfully, ran
`compileall`, and exercised both sample uploads plus file-info and
measurements retrieval over HTTP. This was a manual check, not CI.
