from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import JSON, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.enums import DocumentStatus, JobStatus


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_new_uuid
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    status: Mapped[DocumentStatus] = mapped_column(
        String(20), default=DocumentStatus.PENDING
    )
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=_utcnow, onupdate=_utcnow
    )

    chunks: Mapped[list[Chunk]] = relationship(back_populates="document", cascade="all, delete-orphan")
    jobs: Mapped[list[Job]] = relationship(back_populates="document", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Document {self.filename!r} status={self.status}>"


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_new_uuid
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    index: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    vector: Mapped[list[float] | None] = mapped_column(VECTOR(1024), nullable=True)

    document: Mapped[Document] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<Chunk index={self.index} document_id={self.document_id}>"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_new_uuid
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ingest summary (mode, kept/added/removed chunk counts, embedded count). Null until done.
    stats: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=_utcnow, onupdate=_utcnow
    )

    document: Mapped[Document] = relationship(back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job {self.status} document_id={self.document_id}>"
