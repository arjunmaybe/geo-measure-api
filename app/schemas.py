"""Pydantic response schemas (exact API contract from ARCHITECTURE.md)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: str
    error: str | None
    source_crs: str | None
    measurement_crs: str | None
    crs_assumed: bool
    feature_count: int
    warnings: list[str]
    created_at: datetime

    @computed_field
    @property
    def crs(self) -> str | None:
        """Contract mirror of source_crs (not a stored column)."""
        return self.source_crs


class FeatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: str
    idx: int
    geometry_type: str | None
    geometry_geojson: dict | None
    properties: dict
    geometry_repaired: bool
    status: str
    measure_type: str | None
    measure_value: float | None
    measure_unit: str | None
    message: str | None
    # Owning file's source CRS (never the measurement CRS). Populated by
    # the route from the existing File relationship; no stored column.
    crs: str | None = None


class MeasurementsOut(BaseModel):
    file_id: str
    measurements: list[FeatureOut]


class ErrorOut(BaseModel):
    detail: str
    code: str
