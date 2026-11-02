"""Request/response schemas for the research API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .rights import MAX_PUBLIC_EXCERPT_CHARS, MEDIA_RIGHTS

METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "followers")
BEAT_ROLES = ("hook", "setup", "tension", "evidence", "reveal", "payoff", "cta")
CHANNEL_PLATFORMS = ("youtube", "instagram", "tiktok", "facebook", "x", "linkedin", "threads")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TopicJobCreate(_Strict):
    topic: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    country: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    audience: Optional[str] = Field(default=None, max_length=200)
    style: Optional[str] = Field(default=None, max_length=100)
    lookback_days: Literal[7, 30, 90] = 30
    target_duration_s: int = Field(default=30, ge=10, le=90)
    audience_goal: Literal["views", "saves", "shares", "comments", "follows"] = "views"
    channels: list[str] = Field(default_factory=list, max_length=5)
    clips_per_run: int = Field(default=1, ge=1, le=3)
    daily_budget_usd: Decimal = Field(default=Decimal("0"), ge=0, le=50, decimal_places=2)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item or len(item) > 64:
                raise ValueError("each channel must be 1-64 characters")
            cleaned.append(item)
        return cleaned


class MetricsIn(_Strict):
    observed_at: Optional[datetime] = None
    provider: str = Field(default="manual", min_length=1, max_length=64)
    metric_scope: Literal["public", "estimate", "own_insights"] = "public"
    views: Optional[int] = Field(default=None, ge=0)
    likes: Optional[int] = Field(default=None, ge=0)
    comments: Optional[int] = Field(default=None, ge=0)
    shares: Optional[int] = Field(default=None, ge=0)
    saves: Optional[int] = Field(default=None, ge=0)
    followers: Optional[int] = Field(default=None, ge=0)
    # Why a metric is NULL: "hidden_by_creator", "not_exposed_by_api", ...
    missing_reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("missing_reasons")
    @classmethod
    def _reasons(cls, value: dict[str, str]) -> dict[str, str]:
        for key, reason in value.items():
            if key not in METRIC_FIELDS:
                raise ValueError(f"unknown metric {key!r}")
            if not reason or len(reason) > 120:
                raise ValueError("missing reason must be 1-120 characters")
        return value

    def resolved_missing_reasons(self) -> dict[str, str]:
        """Every NULL metric gets a reason; a supplied value drops a stale one."""
        reasons = {}
        for name in METRIC_FIELDS:
            if getattr(self, name) is None:
                reasons[name] = self.missing_reasons.get(name, "not_provided")
        return reasons


class SourceIn(_Strict):
    source_url: str = Field(min_length=1, max_length=2048)
    creator_handle: Optional[str] = Field(default=None, max_length=100)
    published_at: Optional[datetime] = None
    duration_ms: Optional[int] = Field(default=None, gt=0, le=3_600_000)
    media_rights: str = "metadata_only"
    rights_basis: Optional[str] = Field(default=None, max_length=500)
    license_reference: Optional[str] = Field(default=None, max_length=500)
    provider: str = Field(default="manual", min_length=1, max_length=64)
    provider_version: Optional[str] = Field(default=None, max_length=64)
    # Opening line of the caption or on-screen hook. Untrusted data, never instructions.
    caption_excerpt: Optional[str] = Field(default=None, max_length=MAX_PUBLIC_EXCERPT_CHARS)
    notes: Optional[str] = Field(default=None, max_length=1000)
    # Test fixtures and dry runs must say so, so they never pollute real cohorts.
    is_synthetic: bool = False
    metrics: Optional[MetricsIn] = None

    @field_validator("media_rights")
    @classmethod
    def _rights(cls, value: str) -> str:
        if value not in MEDIA_RIGHTS:
            raise ValueError(f"media_rights must be one of {', '.join(MEDIA_RIGHTS)}")
        return value


class SourceBatchIn(_Strict):
    sources: list[SourceIn] = Field(min_length=1, max_length=50)


class WordIn(_Strict):
    text: str = Field(min_length=1, max_length=100)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _order(self):
        if self.end_ms < self.start_ms:
            raise ValueError("word end_ms must be >= start_ms")
        return self


class CaptionEventIn(_Strict):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=300)


class MediaAnalysisIn(_Strict):
    """Measurements of a rights-cleared video, produced on the remote worker
    (faster-whisper word timestamps, scene_detection cuts)."""

    duration_ms: int = Field(gt=0, le=3_600_000)
    asr_provider: str = Field(min_length=1, max_length=64)
    language: Optional[str] = Field(default=None, max_length=10)
    words: list[WordIn] = Field(default_factory=list, max_length=20_000)
    cuts_ms: list[int] = Field(default_factory=list, max_length=5_000)
    caption_events: list[CaptionEventIn] = Field(default_factory=list, max_length=5_000)
    store_full_transcript: bool = True

    @model_validator(mode="after")
    def _within_duration(self):
        for word in self.words:
            if word.end_ms > self.duration_ms:
                raise ValueError("word timing exceeds duration_ms")
        for cut in self.cuts_ms:
            if not 0 < cut < self.duration_ms:
                raise ValueError("cuts_ms must lie strictly inside the video")
        return self


class ChannelIn(_Strict):
    platform: Literal[CHANNEL_PLATFORMS]  # type: ignore[valid-type]
    display_name: str = Field(min_length=1, max_length=120)
    handle: Optional[str] = Field(default=None, max_length=100)
    # Where posting credentials live: the Upload-Post profile name. OAuth
    # tokens never enter this database.
    upload_post_profile: Optional[str] = Field(default=None, max_length=100)
    niche: Optional[str] = Field(default=None, max_length=200)
    owner_attestation: bool

    @field_validator("owner_attestation")
    @classmethod
    def _attested(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "Only channels you created yourself and control can be registered "
                "(owner_attestation must be true)."
            )
        return value
