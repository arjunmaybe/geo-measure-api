"""SQLAlchemy models for files and features."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _new_uuid() -> str:
    return uuid.uuid4().hex


class File(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PROCESSING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_crs: Mapped[str | None] = mapped_column(String, nullable=True)
    measurement_crs: Mapped[str | None] = mapped_column(String, nullable=True)
    crs_assumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    features: Mapped[list["Feature"]] = relationship(
        "Feature", back_populates="file", cascade="all, delete-orphan"
    )


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry_type: Mapped[str | None] = mapped_column(String, nullable=True)
    geometry_geojson: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    properties: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    geometry_repaired: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    measure_type: Mapped[str | None] = mapped_column(String, nullable=True)
    measure_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    measure_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    file: Mapped[File] = relationship("File", back_populates="features")
