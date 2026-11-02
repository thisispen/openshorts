"""`/api/research` end to end on SQLite: auth, idempotency, caps, rights gate, Jev budget."""

import json
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from research import api, db
from research.service import ResearchService
from research.settings import load_settings
from tests.research_fixtures import owned_video_words, synthetic_sources

TOKEN = "test-research-token-123"
H = {"X-Research-Token": TOKEN}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_API_TOKEN", TOKEN)
    monkeypatch.setenv("RESEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    for name in ("JEV_API_KEY", "JEV_ENABLED", "RESEARCH_DAILY_BUDGET_USD", "RESEARCH_MAX_JOBS_PER_HOUR"):
        monkeypatch.delenv(name, raising=False)

    def factory(jev_handler=None, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        settings = load_settings()
        engine = db._engine_for(settings.database_url)
        db.init_schema(engine)
        transport = httpx.MockTransport(jev_handler) if jev_handler else None
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.get_service] = lambda: ResearchService(engine, load_settings(), jev_transport=transport)
        return TestClient(app)

    yield factory
    db.get_engine().dispose()
    db._engine_for.cache_clear()


def _job(client, **body):
    payload = {"topic": "science myths", **body}
    return client.post("/api/research/topic-jobs", json=payload, headers=H)


# ── auth ────────────────────────────────────────────────────────────────


def test_api_fails_closed_without_configured_token(make_client, monkeypatch):
    client = make_client()
    monkeypatch.delenv("RESEARCH_API_TOKEN")
    assert _job(client).status_code == 503


def test_api_rejects_wrong_token(make_client):
    client = make_client()
    assert client.post("/api/research/topic-jobs", json={"topic": "x"}).status_code == 401
    assert client.post("/api/research/topic-jobs", json={"topic": "x"}, headers={"X-Research-Token": "nope"}).status_code == 401


# ── topic jobs ──────────────────────────────────────────────────────────


def test_topic_job_is_unique_and_idempotent(make_client):
    client = make_client()
    first = _job(client, idempotency_key="intake-0001")
    assert first.status_code == 201
    job_id = first.json()["job_id"]
    again = _job(client, idempotency_key="intake-0001")
    assert again.status_code == 200 and again.json()["job_id"] == job_id and again.json()["created"] is False

    # Same key, different body: refuse instead of silently returning the old job.
    assert _job(client, idempotency_key="intake-0001", language="es").status_code == 409

    # Header key wins; no key at all dedupes identical requests per day.
    other = client.post("/api/research/topic-jobs", json={"topic": "science myths"}, headers={**H, "Idempotency-Key": "header-key-1"})
    assert other.status_code == 201 and other.json()["job_id"] != job_id
    auto1, auto2 = _job(client, topic="ocean facts"), _job(client, topic="ocean facts")
    assert auto1.json()["job_id"] == auto2.json()["job_id"]

    fetched = client.get(f"/api/research/topic-jobs/{job_id}", headers=H).json()
    assert fetched["topic"] == "science myths" and fetched["status"] == "received"


@pytest.mark.parametrize(
    "body",
    [
        {"topic": ""},
        {"topic": "   "},
        {"topic": "x" * 201},
        {"topic": "ok", "clips_per_run": 9},
        {"topic": "ok", "lookback_days": 14},
        {"topic": "ok", "daily_budget_usd": 500},
        {"topic": "ok", "channels": ["a"] * 6},
        {"topic": "ok", "unexpected": True},
    ],
)
def test_topic_job_rejects_invalid_input(make_client, body):
    client = make_client()
    assert client.post("/api/research/topic-jobs", json=body, headers=H).status_code == 422


def test_topic_job_rate_limit(make_client):
    client = make_client(RESEARCH_MAX_JOBS_PER_HOUR="2")
    assert _job(client, topic="a").status_code == 201
    assert _job(client, topic="b").status_code == 201
    assert _job(client, topic="c").status_code == 429
    assert _job(client, topic="a").status_code == 200  # replay is not a new job


# ── sources ─────────────────────────────────────────────────────────────


def test_import_20_synthetic_records_keeps_nulls(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    records = synthetic_sources(20)
    records.append(dict(records[0]))  # duplicate within the batch
    records.append({"source_url": "http://127.0.0.1/reel/abc"})  # SSRF-shaped
    records.append({"source_url": "https://www.instagram.com/reel/Cxyz12345/", "media_rights": "licensed", "rights_basis": "deal"})
    result = client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": records}, headers=H).json()

    assert len(result["imported"]) == 20
    assert [d["index"] for d in result["duplicates"]] == [20]
    assert {r["index"] for r in result["rejected"]} == {21, 22}

    sources = client.get(f"/api/research/topic-jobs/{job_id}/sources", headers=H).json()["sources"]
    assert len(sources) == 20
    hidden = next(s for s in sources if s["latest_metrics"]["likes"] is None)
    assert hidden["latest_metrics"]["missing_reasons"]["likes"] == "hidden_by_creator"
    assert all(s["latest_metrics"]["views"] is None for s in sources)
    assert all(s["latest_metrics"]["missing_reasons"]["views"] == "not_exposed_by_api" for s in sources)
    assert all(s["latest_metrics"]["missing_reasons"]["shares"] == "not_provided" for s in sources)
    assert all("igsh" not in s["canonical_url"] for s in sources)


def test_repeat_observation_adds_snapshot(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    url = "https://www.instagram.com/reel/Crepeat001/"
    client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": [{"source_url": url, "metrics": {"likes": 10}}]}, headers=H)
    second = client.post(
        f"/api/research/topic-jobs/{job_id}/sources",
        json={"sources": [{"source_url": url, "metrics": {"likes": 25, "observed_at": "2099-01-01T00:00:00Z"}}]},
        headers=H,
    ).json()
    assert second["duplicates"][0]["source_id"]
    source = client.get(f"/api/research/topic-jobs/{job_id}/sources", headers=H).json()["sources"][0]
    assert source["latest_metrics"]["likes"] == 25


def test_batch_size_limits(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    assert client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": []}, headers=H).status_code == 422
    too_many = synthetic_sources(51)
    assert client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": too_many}, headers=H).status_code == 422
    assert client.post("/api/research/topic-jobs/missing/sources", json={"sources": synthetic_sources(1)}, headers=H).status_code == 404


# ── media analysis (rights gate) ────────────────────────────────────────


def _add(client, job_id, record):
    result = client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": [record]}, headers=H).json()
    return result["imported"][0]["source_id"]


def test_metadata_only_source_cannot_be_analysed(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    source_id = _add(client, job_id, {"source_url": "https://www.instagram.com/reel/Cpublic001/"})
    response = client.post(
        f"/api/research/sources/{source_id}/media-analysis",
        json={"duration_ms": 30_000, "asr_provider": "faster-whisper", "words": owned_video_words()},
        headers=H,
    )
    assert response.status_code == 403
    source = client.get(f"/api/research/topic-jobs/{job_id}/sources", headers=H).json()["sources"][0]
    assert source["features"] is None


def test_owned_video_yields_words_cuts_and_beats_with_provenance(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    source_id = _add(client, job_id, {
        "source_url": "https://www.youtube.com/shorts/abcdefghijk",
        "media_rights": "owned",
        "rights_basis": "Recorded and edited by the operator, 2026-10",
    })
    response = client.post(
        f"/api/research/sources/{source_id}/media-analysis",
        json={
            "duration_ms": 30_000,
            "asr_provider": "faster-whisper",
            "language": "en",
            "words": owned_video_words(),
            "cuts_ms": [3100, 6300, 8500, 14100, 16900, 24400],
            "caption_events": [{"start_ms": 0, "end_ms": 1500, "text": "Ten percent?"}],
        },
        headers=H,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    features = body["features"]
    assert features["extraction_scope"] == "rights_verified_media"
    assert features["hook_archetype"] == "question"
    assert features["first_cut_seconds"] == 3.1
    assert features["beat_sequence"][0] == "hook" and features["beat_sequence"][-1] == "cta"
    assert 0 < features["quality_score"] <= 1
    beats = body["beats"]
    assert beats[0]["start_ms"] == 0 and beats[-1]["end_ms"] == 30_000

    with db.get_engine().connect() as conn:
        transcript = conn.execute(db.transcripts.select()).mappings().one()
        stored_beats = conn.execute(db.beats.select()).mappings().all()
    assert transcript["full_text"].startswith("Why do we only use")
    assert transcript["rights_basis"].startswith("Recorded")
    assert len(stored_beats) == len(beats)


def test_media_analysis_validates_timing(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    source_id = _add(client, job_id, {"source_url": "https://youtu.be/abcdefghijk", "media_rights": "owned", "rights_basis": "mine"})
    bad = [
        {"duration_ms": 1000, "asr_provider": "x", "words": [{"text": "late", "start_ms": 900, "end_ms": 1500}]},
        {"duration_ms": 1000, "asr_provider": "x", "words": [{"text": "back", "start_ms": 500, "end_ms": 100}]},
        {"duration_ms": 1000, "asr_provider": "x", "cuts_ms": [1000]},
    ]
    for body in bad:
        assert client.post(f"/api/research/sources/{source_id}/media-analysis", json=body, headers=H).status_code == 422


# ── classification + budget ─────────────────────────────────────────────


def test_classify_uses_rules_when_jev_disabled(make_client):
    client = make_client()
    job_id = _job(client).json()["job_id"]
    client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": synthetic_sources(8)}, headers=H)
    result = client.post(f"/api/research/topic-jobs/{job_id}/classify", headers=H).json()
    assert result["classified"] == 8
    assert {r["provider"] for r in result["results"]} == {"fallback"}
    assert all(r["approve_media_processing"] is False for r in result["results"])
    assert all(r["needs_review"] is True for r in result["results"])  # synthetic always reviewed

    cohort = client.get(f"/api/research/topic-jobs/{job_id}/cohort?include_synthetic=true", headers=H).json()
    assert cohort["sample_size"] == 8
    assert cohort["engagement_rate"]["n"] + cohort["engagement_rate"]["n_missing"] == 8
    assert cohort["views"]["n"] == 0  # every view count was NULL
    assert "question" in cohort["hook_archetypes"]
    assert client.get(f"/api/research/topic-jobs/{job_id}/cohort", headers=H).json()["sample_size"] == 0


def _jev_ok(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/systemone"
    assert request.headers["Authorization"] == "Bearer jev-test-key"
    sent = json.loads(request.content)
    assert set(sent["questions"]) == {"hook_type", "topic_fit", "needs_review"}
    return httpx.Response(200, json={
        "model": "jev-latest",
        "answers": {
            "hook_type": {"type": "choice", "choice": "contrarian", "confidence": 0.82, "probabilities": {"contrarian": 0.82}},
            "topic_fit": {"type": "score", "score": 3.4, "confidence": 0.6, "legend": {}, "probabilities": {}},
            "needs_review": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 400, "output_tokens": 9},
    })


def test_jev_call_is_budgeted_and_settled(make_client):
    client = make_client(_jev_ok, JEV_API_KEY="jev-test-key", JEV_ENABLED="true",
                         RESEARCH_DAILY_BUDGET_USD="1", JEV_COST_PER_1K_INPUT_USD="0.01")
    job_id = _job(client, daily_budget_usd=1).json()["job_id"]
    record = {"source_url": "https://www.instagram.com/reel/Cjev000001/", "caption_excerpt": "Stop believing this space myth."}
    client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": [record]}, headers=H)
    result = client.post(f"/api/research/topic-jobs/{job_id}/classify", headers=H).json()["results"][0]
    assert result["provider"] == "jev"
    assert result["hook_archetype"] == "contrarian"
    assert result["needs_review"] is False
    assert result["approve_media_processing"] is False  # policy overrides any model
    spend = client.get("/api/research/spend", headers=H).json()
    assert spend["spent_today_usd"] == pytest.approx(0.004)  # 400 tokens at $0.01/1k


def test_zero_budget_never_calls_jev(make_client):
    def explode(request):  # pragma: no cover - must not be reached
        raise AssertionError("Jev was called with a zero budget")

    client = make_client(explode, JEV_API_KEY="jev-test-key", JEV_ENABLED="true", RESEARCH_DAILY_BUDGET_USD="0")
    job_id = _job(client).json()["job_id"]
    client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": synthetic_sources(2)}, headers=H)
    results = client.post(f"/api/research/topic-jobs/{job_id}/classify", headers=H).json()["results"]
    assert {r["provider"] for r in results} == {"fallback"}
    assert all("budget" in r["error"] for r in results)


def test_malformed_jev_answer_falls_back(make_client):
    client = make_client(lambda request: httpx.Response(200, json={"answers": {"hook_type": {"choice": "nonsense"}}}),
                         JEV_API_KEY="jev-test-key", JEV_ENABLED="true", RESEARCH_DAILY_BUDGET_USD="1")
    job_id = _job(client, daily_budget_usd=1).json()["job_id"]
    client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": synthetic_sources(1)}, headers=H)
    result = client.post(f"/api/research/topic-jobs/{job_id}/classify", headers=H).json()["results"][0]
    assert result["provider"] == "fallback" and result["error"].startswith("jev_error")
    # The failed call is still accounted at its estimate.
    assert client.get("/api/research/spend", headers=H).json()["spent_today_usd"] > 0


# ── channels ────────────────────────────────────────────────────────────


def test_channel_registry_requires_attestation_and_stores_no_tokens(make_client):
    client = make_client()
    body = {"platform": "youtube", "display_name": "Myth Lab", "handle": "@mythlab", "upload_post_profile": "mythlab"}
    assert client.post("/api/research/channels", json={**body, "owner_attestation": False}, headers=H).status_code == 422
    assert client.post("/api/research/channels", json={**body, "owner_attestation": True, "access_token": "x"}, headers=H).status_code == 422
    created = client.post("/api/research/channels", json={**body, "owner_attestation": True}, headers=H)
    assert created.status_code == 201 and created.json()["status"] == "pending_oauth"
    assert client.post("/api/research/channels", json={**body, "owner_attestation": True}, headers=H).status_code == 409
    assert "token" not in " ".join(c.name for c in db.channels.columns)


def test_postgres_ddl_compiles():
    ddl = db.postgres_ddl()
    assert "CREATE TABLE research_topic_jobs" in ddl
    assert "ck_source_rights" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
