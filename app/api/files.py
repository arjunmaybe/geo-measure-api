"""File upload, retrieval, and measurement routes.

HTTP plumbing only: request validation, bounded upload reading, service
calls, and error mapping. CRS/geometry/measurement logic stays in
``app/services/``. Validation precedence: missing upload (400) -> size
limit during chunked reading (413) -> extension (415) -> archive safety
(422, raised by the service with no record left behind).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File as FormFile, UploadFile
from sqlalchemy.orm import Session

from app import config
from app.api.errors import ApiError
from app.db import get_db
from app.models import Feature, File
from app.schemas import FileInfo, MeasurementsOut
from app.services import ingest

router = APIRouter(prefix="/api")

_READ_CHUNK = 1024 * 1024  # 1 MiB bounded reads; never an unbounded body read
_INPUT_EXTS = {".zip", ".kml"}


@router.post("/files/", response_model=FileInfo, status_code=201)
async def upload_file(
    file: UploadFile | None = FormFile(default=None),
    db: Session = Depends(get_db),
) -> File:
    if file is None:
        raise ApiError(400, "No file uploaded", "NO_FILE")
    # Size is enforced while streaming so oversized uploads fail with 413
    # before extension checks and before any database record exists.
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ApiError(
                413,
                f"Upload exceeds {config.MAX_UPLOAD_MB} MB",
                "FILE_TOO_LARGE",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    filename = file.filename or ""
    if Path(filename).suffix.lower() not in _INPUT_EXTS:
        raise ApiError(415, "Unsupported file type", "UNSUPPORTED_TYPE")
    try:
        return ingest.process_upload(filename, data, db)
    except ingest.FileTooLargeError as exc:
        raise ApiError(413, str(exc), "FILE_TOO_LARGE") from exc
    except ingest.UnsupportedTypeError as exc:
        raise ApiError(415, str(exc), "UNSUPPORTED_TYPE") from exc
    except ingest.InvalidArchiveError as exc:
        raise ApiError(422, str(exc), "INVALID_ARCHIVE") from exc


@router.get("/files/{file_id}/", response_model=FileInfo)
def get_file(file_id: str, db: Session = Depends(get_db)) -> File:
    record = db.get(File, file_id)
    if record is None:
        raise ApiError(404, "File not found", "NOT_FOUND")
    return record


@router.get("/files/{file_id}/measurements/", response_model=MeasurementsOut)
def get_measurements(file_id: str, db: Session = Depends(get_db)) -> MeasurementsOut:
    record = db.get(File, file_id)
    if record is None:
        raise ApiError(404, "File not found", "NOT_FOUND")
    features = (
        db.query(Feature)
        .filter_by(file_id=file_id)
        .order_by(Feature.idx)
        .all()
    )
    return MeasurementsOut(file_id=file_id, measurements=features)
