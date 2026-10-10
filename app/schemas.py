"""Pydantic response schemas (exact API contract from ARCHITECTURE.md)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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


class MeasurementsOut(BaseModel):
    file_id: str
    measurements: list[FeatureOut]


class ErrorOut(BaseModel):
    detail: str
    code: str
