"""Central application configuration.

Anchors the default SQLite path to the project root (not the CWD) so
`uvicorn app.main:app` and `pytest` resolve the same database regardless of
where they are launched from. `DATABASE_URL` env var overrides the default.
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_UPLOAD_MB = 25
MAX_ZIP_MEMBERS = 50
MAX_UNCOMPRESSED_MB = 200

ALLOWED_SHP_EXT = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
}

# Project root = parent of the `app` package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default SQLite file lives at the project root: <root>/geo_measure.db
DEFAULT_DB_PATH = PROJECT_ROOT / "geo_measure.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DB_PATH}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
