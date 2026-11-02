"""Offline smoke run of the research pipeline: no network, no paid calls, no media.

    python -m research.dry_run

Topic job -> 10 synthetic metadata records (marked synthetic) -> one owned
video's measured timing -> rules classification -> cohort report, all on a
throwaway SQLite file. It stops where script writing and rendering begin.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from tests.research_fixtures import owned_video_words, synthetic_sources

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RESEARCH_DATABASE_URL"] = f"sqlite:///{Path(tmp, 'dry_run.db').as_posix()}"
        os.environ["RESEARCH_API_TOKEN"] = "dry-run-token"
        os.environ["JEV_ENABLED"] = "false"

        from research import api, db

        app = FastAPI()
        app.include_router(api.router)
        client = TestClient(app)
        h = {"X-Research-Token": "dry-run-token"}

        job = client.post("/api/research/topic-jobs", json={"topic": "science myths", "idempotency_key": "dry-run-0001"}, headers=h).json()
        job_id = job["job_id"]
        print(f"1. topic job        {job_id} ({job['status']})")

        imported = client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": synthetic_sources(10)}, headers=h).json()
        print(f"2. metadata records imported={len(imported['imported'])} duplicates={len(imported['duplicates'])} rejected={len(imported['rejected'])}")

        owned = client.post(f"/api/research/topic-jobs/{job_id}/sources", json={"sources": [{
            "source_url": "https://www.youtube.com/shorts/ownedSample",
            "media_rights": "owned",
            "rights_basis": "Dry-run fixture standing in for a video the operator recorded",
            "is_synthetic": True,
        }]}, headers=h).json()["imported"][0]["source_id"]
        analysis = client.post(f"/api/research/sources/{owned}/media-analysis", json={
            "duration_ms": 30_000, "asr_provider": "fixture", "words": owned_video_words(),
            "cuts_ms": [3100, 6300, 8500, 14100, 16900, 24400],
        }, headers=h).json()
        f = analysis["features"]
        print(f"3. owned video      wpm={f['speech_wpm']} silence={f['silence_ratio']} median_shot={f['median_shot_seconds']}s hook={f['hook_archetype']}")
        print("   beats            " + " -> ".join(f"{b['start_ms'] / 1000:g}-{b['end_ms'] / 1000:g}s {b['role']}" for b in analysis["beats"]))

        classified = client.post(f"/api/research/topic-jobs/{job_id}/classify", headers=h).json()
        providers = {r["provider"] for r in classified["results"]}
        print(f"4. classified       {classified['classified']} via {sorted(providers)}")

        cohort = client.get(f"/api/research/topic-jobs/{job_id}/cohort?include_synthetic=true", headers=h).json()
        print(f"5. cohort           n={cohort['sample_size']} hooks={json.dumps(cohort['hook_archetypes'])}")
        print(f"   engagement       {json.dumps(cohort['engagement_rate'])}")
        for caveat in cohort["caveats"]:
            print(f"   caveat           {caveat}")
        print(f"6. spend today      ${client.get('/api/research/spend', headers=h).json()['spent_today_usd']}")
        # Windows keeps the SQLite file locked until the pool lets go of it.
        db.get_engine().dispose()
        db._engine_for.cache_clear()
    print("Dry run finished: nothing downloaded, posted or billed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
