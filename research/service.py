"""Research workflows over the SQLAlchemy Core tables (sync; FastAPI runs it in a threadpool)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Engine

from . import db
from .beats import abstract_template, segment_beats
from .cohort import cohort_report
from .hook_taxonomy import classify_hook
from .jev import (
    JevClient,
    JevError,
    build_questions,
    build_state,
    estimate_cost_usd,
    estimate_input_tokens,
    fallback_decisions,
    parse_answers,
    policy_gate,
)
from .models import METRIC_FIELDS, ChannelIn, MediaAnalysisIn, SourceBatchIn, TopicJobCreate
from .rights import RightsError, can_store_full_transcript, require_media_rights, validate_rights_fields
from .settings import ResearchSettings
from .timing import analyze_captions, analyze_cuts, analyze_speech
from .urls import UnsafeUrlError, parse_source_url

EXTRACTION_VERSION = "media_v1"


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class RateLimited(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row(mapping) -> dict:
    out = dict(mapping)
    for key, value in out.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def request_hash(req: TopicJobCreate) -> str:
    body = req.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class ResearchService:
    def __init__(self, engine: Engine, settings: ResearchSettings, *, jev_transport: httpx.BaseTransport | None = None):
        self.engine = engine
        self.settings = settings
        self._jev_transport = jev_transport

    # ── topic jobs ──────────────────────────────────────────────────────

    def create_topic_job(self, req: TopicJobCreate, header_key: str | None = None) -> tuple[dict, bool]:
        digest = request_hash(req)
        # Without a client key, an identical request on the same UTC day is the
        # same job: n8n retries and double-submits collapse into one.
        key = header_key or req.idempotency_key or f"auto:{digest[:32]}:{utcnow():%Y%m%d}"
        with self.engine.begin() as conn:
            existing = conn.execute(select(db.topic_jobs).where(db.topic_jobs.c.idempotency_key == key)).mappings().first()
            if existing:
                if existing["request_hash"] != digest:
                    raise Conflict("This idempotency key was already used with a different request body.")
                return _row(existing), False
            recent = conn.execute(
                select(func.count()).select_from(db.topic_jobs).where(db.topic_jobs.c.created_at >= utcnow() - timedelta(hours=1))
            ).scalar_one()
            if recent >= self.settings.max_jobs_per_hour:
                raise RateLimited(f"At most {self.settings.max_jobs_per_hour} topic jobs per hour.")
            now = utcnow()
            values = {
                **req.model_dump(exclude={"idempotency_key"}),
                "id": str(uuid.uuid4()),
                "idempotency_key": key,
                "request_hash": digest,
                "status": "received",
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(insert(db.topic_jobs).values(**values))
            created = conn.execute(select(db.topic_jobs).where(db.topic_jobs.c.id == values["id"])).mappings().one()
        return _row(created), True

    def get_topic_job(self, job_id: str) -> dict:
        with self.engine.connect() as conn:
            job = conn.execute(select(db.topic_jobs).where(db.topic_jobs.c.id == job_id)).mappings().first()
            if not job:
                raise NotFound("Topic job not found")
            counts = conn.execute(
                select(db.sources.c.media_rights, func.count()).where(db.sources.c.topic_job_id == job_id).group_by(db.sources.c.media_rights)
            ).all()
        return {**_row(job), "source_counts": {rights: n for rights, n in counts}}

    def _set_status(self, conn, job_id: str, status: str) -> None:
        conn.execute(update(db.topic_jobs).where(db.topic_jobs.c.id == job_id).values(status=status, updated_at=utcnow()))

    # ── sources and public metrics ─────────────────────────────────────

    def add_sources(self, job_id: str, batch: SourceBatchIn) -> dict:
        """Store metadata records. Never downloads or fetches the URLs."""
        imported, duplicates, rejected = [], [], []
        with self.engine.begin() as conn:
            if not conn.execute(select(db.topic_jobs.c.id).where(db.topic_jobs.c.id == job_id)).first():
                raise NotFound("Topic job not found")
            seen: set[str] = set()
            for index, record in enumerate(batch.sources):
                try:
                    parsed = parse_source_url(record.source_url)
                    validate_rights_fields(record.media_rights, record.rights_basis, record.license_reference)
                except (UnsafeUrlError, RightsError) as exc:
                    rejected.append({"index": index, "reason": str(exc)})
                    continue
                existing_id = conn.execute(
                    select(db.sources.c.id).where(and_(db.sources.c.topic_job_id == job_id, db.sources.c.canonical_url == parsed.canonical_url))
                ).scalar()
                if parsed.canonical_url in seen or existing_id:
                    duplicates.append({"index": index, "canonical_url": parsed.canonical_url, "source_id": existing_id})
                    if existing_id and record.metrics:
                        # A repeat observation of a known item is a new snapshot, not a duplicate row.
                        self._insert_metrics(conn, existing_id, record.metrics)
                    continue
                seen.add(parsed.canonical_url)
                source_id = str(uuid.uuid4())
                conn.execute(insert(db.sources).values(
                    id=source_id,
                    topic_job_id=job_id,
                    platform=parsed.platform,
                    content_type=parsed.content_type,
                    external_id=parsed.external_id,
                    source_url=record.source_url,
                    canonical_url=parsed.canonical_url,
                    creator_handle=record.creator_handle or parsed.creator_handle,
                    published_at=record.published_at,
                    discovered_at=utcnow(),
                    duration_ms=record.duration_ms,
                    media_rights=record.media_rights,
                    rights_basis=record.rights_basis,
                    license_reference=record.license_reference,
                    provider=record.provider,
                    provider_version=record.provider_version,
                    caption_excerpt=record.caption_excerpt,
                    notes=record.notes,
                    is_synthetic=record.is_synthetic,
                ))
                if record.metrics:
                    self._insert_metrics(conn, source_id, record.metrics)
                imported.append({"index": index, "source_id": source_id, "platform": parsed.platform, "canonical_url": parsed.canonical_url})
            if imported:
                self._set_status(conn, job_id, "researching")
        return {"imported": imported, "duplicates": duplicates, "rejected": rejected}

    def _insert_metrics(self, conn, source_id: str, metrics) -> None:
        conn.execute(insert(db.metric_snapshots).values(
            source_id=source_id,
            observed_at=metrics.observed_at or utcnow(),
            provider=metrics.provider,
            metric_scope=metrics.metric_scope,
            missing_reasons=metrics.resolved_missing_reasons(),
            **{name: getattr(metrics, name) for name in METRIC_FIELDS},
        ))

    def _latest_metrics(self, conn, source_ids: list[str]) -> dict[str, dict]:
        if not source_ids:
            return {}
        rows = conn.execute(
            select(db.metric_snapshots).where(db.metric_snapshots.c.source_id.in_(source_ids))
            .order_by(db.metric_snapshots.c.source_id, db.metric_snapshots.c.observed_at.desc(), db.metric_snapshots.c.id.desc())
        ).mappings().all()
        latest: dict[str, dict] = {}
        for row in rows:
            latest.setdefault(row["source_id"], _row(row))
        return latest

    def list_sources(self, job_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(db.sources).where(db.sources.c.topic_job_id == job_id).order_by(db.sources.c.discovered_at)).mappings().all()
            ids = [r["id"] for r in rows]
            metrics = self._latest_metrics(conn, ids)
            feats = {
                r["source_id"]: _row(r)
                for r in conn.execute(select(db.features).where(db.features.c.source_id.in_(ids))).mappings().all()
            } if ids else {}
        return [{**_row(r), "latest_metrics": metrics.get(r["id"]), "features": feats.get(r["id"])} for r in rows]

    def _source(self, conn, source_id: str) -> dict:
        source = conn.execute(select(db.sources).where(db.sources.c.id == source_id)).mappings().first()
        if not source:
            raise NotFound("Source not found")
        return dict(source)

    # ── rights-gated media analysis ────────────────────────────────────

    def record_media_analysis(self, source_id: str, analysis: MediaAnalysisIn) -> dict:
        with self.engine.begin() as conn:
            source = self._source(conn, source_id)
            # Policy before anything else: metadata-only items never reach here.
            require_media_rights(source["media_rights"], source["rights_basis"])

            duration = analysis.duration_ms
            words = [w.model_dump() for w in analysis.words]
            speech = analyze_speech(words, duration)
            cuts = analyze_cuts(analysis.cuts_ms, duration)
            captions = analyze_captions([c.model_dump() for c in analysis.caption_events], duration)
            beat_map = segment_beats(words, duration) if words else []
            template = abstract_template(beat_map, duration) if beat_map else None
            hook = classify_hook(speech.get("hook_0_3s")) if speech["status"] == "measured" else None

            measured = [part["status"] == "measured" for part in (speech, cuts, captions)]
            beat_conf = sum(b["confidence"] for b in beat_map) / len(beat_map) if beat_map else 0.0
            quality = round((sum(measured) / 3) * (0.5 + 0.5 * beat_conf), 4)

            hook_end = next((b["end_ms"] for b in beat_map if b["role"] == "hook"), None)
            row = {
                "source_id": source_id,
                "extraction_scope": "rights_verified_media",
                "extraction_version": EXTRACTION_VERSION,
                "hook_archetype": hook["archetype"] if hook else None,
                "hook_confidence": hook["confidence"] if hook else None,
                "hook_method": hook["method"] if hook else None,
                "hook_seconds": hook_end / 1000 if hook_end is not None else None,
                "first_cut_seconds": cuts["first_cut_ms"] / 1000 if cuts["status"] == "measured" else None,
                "median_shot_seconds": cuts["median_shot_ms"] / 1000 if cuts["status"] == "measured" else None,
                "speech_wpm": speech.get("speech_wpm"),
                "silence_ratio": speech.get("silence_ratio"),
                "caption_density": captions.get("caption_words_per_second"),
                "beat_sequence": template["sequence"] if template else None,
                "measurements": {"speech": speech, "cuts": cuts, "captions": captions, "template": template},
                "quality_score": quality,
                "extracted_at": utcnow(),
            }
            conn.execute(delete(db.features).where(db.features.c.source_id == source_id))
            conn.execute(insert(db.features).values(**row))
            conn.execute(delete(db.beats).where(db.beats.c.source_id == source_id))
            for beat in beat_map:
                conn.execute(insert(db.beats).values(
                    source_id=source_id, start_ms=beat["start_ms"], end_ms=beat["end_ms"],
                    role=beat["role"], confidence=beat["confidence"], evidence=beat["evidence"],
                ))
            if words:
                keep_text = analysis.store_full_transcript and can_store_full_transcript(source["media_rights"])
                conn.execute(delete(db.transcripts).where(db.transcripts.c.source_id == source_id))
                conn.execute(insert(db.transcripts).values(
                    source_id=source_id,
                    asr_provider=analysis.asr_provider,
                    language=analysis.language,
                    full_text=" ".join(w["text"] for w in words) if keep_text else None,
                    words=words,
                    rights_basis=source["rights_basis"],
                    created_at=utcnow(),
                ))
            if source["duration_ms"] is None:
                conn.execute(update(db.sources).where(db.sources.c.id == source_id).values(duration_ms=duration))
        return {"source_id": source_id, "features": _row(row), "beats": beat_map}

    # ── classification (Jev or rules) + budget ─────────────────────────

    def _daily_cap(self, job: dict) -> Decimal:
        cap = self.settings.daily_budget_usd
        job_cap = Decimal(str(job.get("daily_budget_usd") or 0))
        return min(cap, job_cap) if job_cap > 0 else cap

    def spent_today(self, conn) -> Decimal:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = conn.execute(
            select(db.spend.c.status, db.spend.c.estimated_usd, db.spend.c.actual_usd).where(db.spend.c.created_at >= start)
        ).all()
        total = Decimal("0")
        for status, estimated, actual in rows:
            if status == "reserved":
                total += Decimal(str(estimated))
            elif status == "settled":
                total += Decimal(str(actual if actual is not None else estimated))
        return total

    def reserve_spend(self, provider: str, purpose: str, job: dict, estimate: Decimal) -> int:
        cap = self._daily_cap(job)
        with self.engine.begin() as conn:
            if self.spent_today(conn) + estimate > cap:
                raise BudgetExceeded(f"Daily research budget of ${cap} would be exceeded.")
            result = conn.execute(insert(db.spend).values(
                provider=provider, purpose=purpose, topic_job_id=job["id"],
                estimated_usd=estimate, status="reserved", created_at=utcnow(),
            ))
            return int(result.inserted_primary_key[0])

    def settle_spend(self, spend_id: int, actual: Decimal | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(db.spend).where(db.spend.c.id == spend_id).values(status="settled", actual_usd=actual))

    def _decide(self, job: dict, source: dict) -> tuple[dict, str, str | None, int, Decimal]:
        """Returns (decisions, provider, error, input_tokens, cost)."""
        if not self.settings.jev_active:
            return fallback_decisions(job["topic"], source), "fallback", "jev_disabled", 0, Decimal("0")
        client = JevClient(self.settings.jev_api_key, self.settings.jev_model, self.settings.jev_base_url, transport=self._jev_transport)
        payload = client.payload(build_state(job["topic"], source), build_questions())
        tokens = estimate_input_tokens(payload)
        if tokens > self.settings.jev_max_input_tokens:
            return fallback_decisions(job["topic"], source), "fallback", "input_too_large", 0, Decimal("0")
        estimate = estimate_cost_usd(tokens, self.settings.jev_cost_per_1k_input_usd)
        try:
            spend_id = self.reserve_spend("jev", "hook_classification", job, estimate)
        except BudgetExceeded as exc:
            return fallback_decisions(job["topic"], source), "fallback", str(exc), 0, Decimal("0")
        try:
            body = client.ask(payload)
            decisions = parse_answers(body)
        except JevError as exc:
            # A failed call may still have been billed: settle at the estimate.
            self.settle_spend(spend_id, estimate)
            return fallback_decisions(job["topic"], source), "fallback", f"jev_error: {exc}", 0, estimate
        used = decisions.pop("input_tokens")
        actual = estimate_cost_usd(used, self.settings.jev_cost_per_1k_input_usd) if used else estimate
        self.settle_spend(spend_id, actual)
        return decisions, "jev", None, used, actual

    def classify_sources(self, job_id: str) -> dict:
        job = self.get_topic_job(job_id)
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(select(db.sources).where(db.sources.c.topic_job_id == job_id)).mappings().all()]
            measured = {
                r for r, in conn.execute(
                    select(db.features.c.source_id).where(db.features.c.extraction_scope == "rights_verified_media")
                ).all()
            }
        results = []
        for source in rows:
            decisions, provider, error, tokens, cost = self._decide(job, source)
            gated = policy_gate(source, decisions)
            with self.engine.begin() as conn:
                conn.execute(insert(db.decisions).values(
                    source_id=source["id"], provider=provider,
                    model=self.settings.jev_model if provider == "jev" else "rules_v1",
                    decisions=gated, error=error, input_tokens=tokens, cost_usd=cost, created_at=utcnow(),
                ))
                # Measured features win over text-only classification.
                if source["id"] not in measured:
                    conn.execute(delete(db.features).where(db.features.c.source_id == source["id"]))
                    conn.execute(insert(db.features).values(
                        source_id=source["id"],
                        extraction_scope="public_metadata",
                        extraction_version="public_v1",
                        hook_archetype=gated["hook_archetype"],
                        hook_confidence=gated["hook_confidence"],
                        hook_method=provider,
                        measurements={"note": "Text-only: timing, pacing and beat fields are unknown (NULL)."},
                        extracted_at=utcnow(),
                    ))
            results.append({"source_id": source["id"], "provider": provider, "error": error, **gated})
        with self.engine.begin() as conn:
            self._set_status(conn, job_id, "analysed")
        return {"classified": len(results), "results": results}

    def cohort(self, job_id: str, include_synthetic: bool = False) -> dict:
        self.get_topic_job(job_id)
        rows = self.list_sources(job_id)
        report = cohort_report(
            [{"metrics": r["latest_metrics"] or {}, "features": r["features"], "is_synthetic": r["is_synthetic"]} for r in rows],
            include_synthetic=include_synthetic,
        )
        observed = [r["latest_metrics"]["observed_at"] for r in rows if r["latest_metrics"]]
        report["observed_between"] = [min(observed), max(observed)] if observed else None
        return report

    def spend_summary(self) -> dict:
        with self.engine.connect() as conn:
            spent = self.spent_today(conn)
        return {"spent_today_usd": float(spent), "daily_cap_usd": float(self.settings.daily_budget_usd)}

    # ── channel registry (no tokens stored) ────────────────────────────

    def register_channel(self, req: ChannelIn) -> dict:
        values = {
            "id": str(uuid.uuid4()),
            "platform": req.platform,
            "display_name": req.display_name,
            "handle": req.handle,
            "upload_post_profile": req.upload_post_profile,
            "niche": req.niche,
            "owner_attested_at": utcnow(),
            "status": "pending_oauth",
            "created_at": utcnow(),
        }
        with self.engine.begin() as conn:
            if req.handle and conn.execute(
                select(db.channels.c.id).where(and_(db.channels.c.platform == req.platform, db.channels.c.handle == req.handle))
            ).first():
                raise Conflict("That channel is already registered.")
            conn.execute(insert(db.channels).values(**values))
        return _row(values)

    def list_channels(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(select(db.channels).order_by(db.channels.c.created_at)).mappings().all()]
