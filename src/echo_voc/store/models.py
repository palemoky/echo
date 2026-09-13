from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 1024  # bge-m3; changing it requires a new migration


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(Text)
    # track_all=False: only works added explicitly are synced (no account-wide listing).
    track_all: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    comments_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    danmaku_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Feedback(Base):
    """A comment, reply, danmaku or store review."""

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("platform", "kind", "external_id"),
        Index("ix_feedback_work_created", "work_id", "created_at"),
        Index("ix_feedback_created", "created_at"),
        Index(
            "ix_feedback_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(128))
    parent_external_id: Mapped[str | None] = mapped_column(String(128))
    author_hash: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(Float)
    likes: Mapped[int | None] = mapped_column(Integer)
    app_version: Mapped[str | None] = mapped_column(String(64))
    progress_ms: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16))
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_model: Mapped[str | None] = mapped_column(String(128))


class Topic(Base):
    """A canonical feedback theme (issue catalog), maintained per account."""

    __tablename__ = "topics"
    __table_args__ = (
        Index(
            "ix_topics_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    severity: Mapped[int | None] = mapped_column(Integer)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeedbackTopic(Base):
    __tablename__ = "feedback_topics"

    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("feedback.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Snapshot(Base):
    """Structured memory: feedback stats for one period / version."""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "work_id", "label", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # works | comments | danmaku
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    new_items: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    seen_items: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
