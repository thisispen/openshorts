"""Research tables (SQLAlchemy Core). PostgreSQL in production, SQLite for tests/dry runs.

`python -m research.db --print-ddl` prints the PostgreSQL DDL for review
before it is applied; `init_schema` creates missing tables only.
"""

from __future__ import annotations

import sys
from functools import lru_cache

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from .settings import load_settings

metadata = MetaData()
_ID = lambda: String(36)  # noqa: E731 - UUID text, the convention across both apps
_NOW = dict(nullable=False, server_default=func.now())

topic_jobs = Table(
    "research_topic_jobs", metadata,
    Column("id", _ID(), primary_key=True),
    Column("idempotency_key", String(128), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("topic", String(200), nullable=False),
    Column("language", String(10), nullable=False),
    Column("country", String(2)),
    Column("audience", String(200)),
    Column("style", String(100)),
    Column("lookback_days", Integer, nullable=False),
    Column("target_duration_s", Integer, nullable=False),
    Column("audience_goal", String(20), nullable=False),
    Column("channels", JSON, nullable=False),
    Column("clips_per_run", Integer, nullable=False),
    Column("daily_budget_usd", Numeric(10, 2), nullable=False),
    Column("status", String(20), nullable=False, server_default="received"),
    Column("created_at", DateTime(timezone=True), **_NOW),
    Column("updated_at", DateTime(timezone=True), **_NOW),
    CheckConstraint("status IN ('received','researching','analysed','scripting','failed','cancelled')", name="ck_topic_job_status"),
    CheckConstraint("lookback_days IN (7, 30, 90)", name="ck_topic_job_lookback"),
)

sources = Table(
    "research_sources", metadata,
    Column("id", _ID(), primary_key=True),
    Column("topic_job_id", _ID(), ForeignKey("research_topic_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("platform", String(20), nullable=False),
    Column("content_type", String(20), nullable=False),
    Column("external_id", String(100)),
    Column("source_url", Text, nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("creator_handle", String(100)),
    Column("published_at", DateTime(timezone=True)),
    Column("discovered_at", DateTime(timezone=True), **_NOW),
    Column("duration_ms", Integer),
    Column("media_rights", String(30), nullable=False, server_default="metadata_only"),
    Column("rights_basis", Text),
    Column("license_reference", Text),
    Column("provider", String(64), nullable=False),
    Column("provider_version", String(64)),
    Column("caption_excerpt", String(300)),
    Column("notes", Text),
    Column("is_synthetic", Boolean, nullable=False, server_default="false"),
    UniqueConstraint("topic_job_id", "canonical_url", name="uq_research_source_job_url"),
    CheckConstraint("media_rights IN ('metadata_only','owned','licensed','permission_granted')", name="ck_source_rights"),
    CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="ck_source_duration"),
)

metric_snapshots = Table(
    "research_metric_snapshots", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("source_id", _ID(), ForeignKey("research_sources.id", ondelete="CASCADE"), nullable=False),
    Column("observed_at", DateTime(timezone=True), **_NOW),
    Column("provider", String(64), nullable=False),
    Column("metric_scope", String(20), nullable=False),
    # NULL means not observed. Zero means observed as zero.
    Column("views", BigInteger), Column("likes", BigInteger), Column("comments", BigInteger),
    Column("shares", BigInteger), Column("saves", BigInteger), Column("followers", BigInteger),
    Column("missing_reasons", JSON, nullable=False),
    CheckConstraint("metric_scope IN ('public','estimate','own_insights')", name="ck_metric_scope"),
    *[CheckConstraint(f"{m} IS NULL OR {m} >= 0", name=f"ck_metric_{m}") for m in ("views", "likes", "comments", "shares", "saves", "followers")],
)
Index("ix_research_metrics_source_observed", metric_snapshots.c.source_id, metric_snapshots.c.observed_at)

transcripts = Table(
    "research_transcripts", metadata,
    Column("source_id", _ID(), ForeignKey("research_sources.id", ondelete="CASCADE"), primary_key=True),
    Column("asr_provider", String(64), nullable=False),
    Column("language", String(10)),
    # Only for rights-cleared media (enforced in the service, recorded here).
    Column("full_text", Text),
    Column("words", JSON, nullable=False),
    Column("rights_basis", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), **_NOW),
)

features = Table(
    "research_features", metadata,
    Column("source_id", _ID(), ForeignKey("research_sources.id", ondelete="CASCADE"), primary_key=True),
    Column("extraction_scope", String(30), nullable=False),
    Column("extraction_version", String(30), nullable=False),
    Column("hook_archetype", String(30)),
    Column("hook_confidence", Numeric(5, 4)),
    Column("hook_method", String(30)),
    Column("hook_seconds", Numeric(8, 3)),
    Column("first_cut_seconds", Numeric(8, 3)),
    Column("median_shot_seconds", Numeric(8, 3)),
    Column("speech_wpm", Numeric(10, 3)),
    Column("silence_ratio", Numeric(6, 5)),
    Column("caption_density", Numeric(10, 3)),
    Column("beat_sequence", JSON),
    Column("measurements", JSON, nullable=False),
    Column("quality_score", Numeric(5, 4)),
    Column("extracted_at", DateTime(timezone=True), **_NOW),
    CheckConstraint("extraction_scope IN ('public_metadata','rights_verified_media')", name="ck_feature_scope"),
    CheckConstraint("quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)", name="ck_feature_quality"),
)

beats = Table(
    "research_beats", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("source_id", _ID(), ForeignKey("research_sources.id", ondelete="CASCADE"), nullable=False),
    Column("start_ms", Integer, nullable=False),
    Column("end_ms", Integer, nullable=False),
    Column("role", String(20), nullable=False),
    Column("confidence", Numeric(5, 4)),
    Column("evidence", String(100)),
    CheckConstraint("start_ms >= 0 AND end_ms > start_ms", name="ck_beat_bounds"),
)
Index("ix_research_beats_source_start", beats.c.source_id, beats.c.start_ms)

decisions = Table(
    "research_decisions", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("source_id", _ID(), ForeignKey("research_sources.id", ondelete="CASCADE"), nullable=False),
    Column("provider", String(20), nullable=False),  # jev | fallback
    Column("model", String(64)),
    Column("decisions", JSON, nullable=False),
    Column("error", Text),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), **_NOW),
)

spend = Table(
    "research_spend", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("provider", String(32), nullable=False),
    Column("purpose", String(64), nullable=False),
    Column("topic_job_id", _ID()),
    Column("estimated_usd", Numeric(12, 6), nullable=False),
    Column("actual_usd", Numeric(12, 6)),
    Column("status", String(20), nullable=False, server_default="reserved"),
    Column("created_at", DateTime(timezone=True), **_NOW),
    CheckConstraint("status IN ('reserved','settled','released')", name="ck_spend_status"),
)

channels = Table(
    "research_channels", metadata,
    Column("id", _ID(), primary_key=True),
    Column("platform", String(20), nullable=False),
    Column("display_name", String(120), nullable=False),
    Column("handle", String(100)),
    Column("upload_post_profile", String(100)),
    Column("niche", String(200)),
    Column("owner_attested_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False, server_default="pending_oauth"),
    Column("created_at", DateTime(timezone=True), **_NOW),
    UniqueConstraint("platform", "handle", name="uq_research_channel_handle"),
    CheckConstraint("status IN ('pending_oauth','active','paused','revoked')", name="ck_channel_status"),
)


@lru_cache(maxsize=4)
def _engine_for(url: str) -> Engine:
    kwargs = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    return _engine_for(load_settings().database_url)


def init_schema(engine: Engine | None = None) -> None:
    metadata.create_all(engine or get_engine())


def postgres_ddl() -> str:
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    out = []
    for table in metadata.sorted_tables:
        out.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in table.indexes:
            out.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    return "\n\n".join(out)


if __name__ == "__main__":  # pragma: no cover
    if "--print-ddl" in sys.argv:
        print(postgres_ddl())
    elif "--init" in sys.argv:
        init_schema()
        print("research schema ready")
    else:
        print("usage: python -m research.db --print-ddl | --init")
