# NOTES.md — interview study aid (plain language)

## app/config.py
- Holds upload/archive limits and allowed shapefile extensions in one place.
- Default SQLite path is anchored to the project root, not the working directory.
- `DATABASE_URL` env var overrides the default for tests and deploys.

## app/services/crs.py
- Resolves source CRS (KML→4326, shapefile .prj, or cautious 4326 assumption).
- Picks one measurement CRS per file: keep metre-projected, except non-transverse Mercator methods (3857/3785/900913/3395 via method name, EPSG list as fallback — not retained; normal UTM/6933 selection applies), else UTM by bbox centre.
- Uses EPSG:6933 outside UTM latitudes and warns on wide longitude spans.
- Every transformer uses `always_xy=True` to avoid lat/lon swap bugs.
- Limitation: polar check uses the bbox centre, so data can extend past 84°N/-80°S while still picking UTM.

## app/services/measure.py
- Measures only already-projected geometries; it cannot tell CRS from a Shapely object.
- Returns `{"type","value","unit"}` dicts with `m2`/`m` units or `None` when not applicable.
- Rejects unknown kinds and non-finite values; always returns builtin floats.

## app/services/readers.py
- Reads shapefiles and KML with `pyogrio.raw.read` (numpy only), so GeoPandas is never needed at runtime.
- KML is read across every GDAL layer (one per folder) with continuous indices, so multi-folder placemarks are never silently dropped; then falls back to a small secure lxml parser (no network, no entities) for Point/LineString/Polygon incl. holes.
- Normalizes every property to JSON-safe values and reports parse errors without internal paths.
- Limitation: pyogrio returns KML driver field names (e.g. capital `Name`), so property keys differ from the lxml path.

## app/services/geometry.py
- `prepare` keeps missing/empty as None (EMPTY downstream), repairs invalid shapes with `make_valid`, and marks unrepairable ones for ERROR.
- `classify` maps Polygon→area, LineString→length, Point/empty→none, anything else→unsupported.
- Callers run `classify` on the prepared (possibly repaired) output before measuring, since repair can change the type; the ingestion lifecycle is prepare → classify → transform → measure.

## app/models.py and app/db.py
- Two tables: `files` (UUID string id, statuses, CRS labels, warnings JSON) and `features` (indexed file_id FK, GeoJSON/properties JSON, measurement columns).
- SQLite foreign keys enforced on every connection; isolated file-backed engines per test; `create_all` is idempotent so app restarts keep data.

## app/services/ingest.py
- `safe_extract` checks member count, declared size, and streamed bytes before/during extraction; rejects traversal, absolute paths, symlinks, backslashes; needs exactly one `.shp` plus `.shx`/`.dbf`; wipes partial output on error.
- `process_upload` validates archive safety before creating any DB record (so 422 leaves nothing behind), then persists PROCESSING and finalizes COMPLETED/FAILED.
- One measurement CRS per file; each feature is prepared, reprojected with `always_xy=True`, measured, and committed independently — a single ERROR never aborts the rest.
- Empty but readable input with a resolvable CRS completes with zero features and a null measurement CRS instead of an invented UTM zone.
- A non-empty feature with unusable bounds (no file-wide usable bounds) is recorded as ERROR with no value/unit and is never passed to `measure()` without a measurement CRS.

## Sample data (data/ + scripts/generate_sample_data.py)
- Fixed lon/lat coords near (10, 50): KML has a square with a hole, a road, and a point; the shapefile ZIP holds two parcels (shapefiles allow one geometry type per layer).
- KML is hand-written XML; the shapefile is written via GeoPandas (sample-gen only, documented in the script) and zipped with fixed timestamps.
- Both files were verified with the real app readers (pyogrio primary and lxml fallback) before any ingestion test used them.
- `verify_samples.py` reads the sample ZIP through the app's own `safe_extract`, so samples are checked under production upload rules.

## app/schemas.py, app/api/files.py, and app/main.py
- Schemas mirror the contract field-for-field with `from_attributes` ORM serialization; error bodies are always flat `{"detail", "code"}`.
- Routes do HTTP plumbing only: the upload field is optional (missing → 400), size is enforced while re-reading the received file in 1 MiB chunks (413 before extension checks), and service exceptions map to 415/422; COMPLETED and FAILED both return 201.
- Handlers cover `ApiError`, request-validation errors (`VALIDATION_ERROR`), and unexpected exceptions (generic 500, no tracebacks/paths).
- Tests override the `get_db` dependency with isolated file-backed SQLite databases; lifespan startup may create the gitignored default DB, which is local-dev only.
