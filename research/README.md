# Research module (`/api/research`)

Topic-to-channel research for original short-form videos. It turns a topic into a
persisted job, stores public Reel metadata with provenance, measures rhythm and
narrative structure on media the operator owns or licenses, classifies hooks
(Jev or rules), and reports niche cohorts with sample sizes and uncertainty.

It never downloads third-party media, never publishes, and never spends without a
budget reservation.

## Architecture decision

| Responsibility | Where it lives | Why |
|---|---|---|
| Rendering, clipping, AI Shorts, publishing, MCP | OpenShorts core (`app.py`, `saasshorts.py`, Upload-Post) | Already built and tested. Reused, not rewritten |
| Research data, rights gate, analysis | `research/` (this package, MIT) | Missing from every source repo. Kept out of `cloud/` (commercial licence) |
| Storage | PostgreSQL via SQLAlchemy Core; SQLite for tests/dry runs | Self-host OpenShorts has no DB; `cloud/database.py` is commercial, so it isn't reused |
| Orchestration | n8n (`examples/n8n/clip-factory-topic-intake.json` + the official examples) | n8n calls the API; heavy work stays on the server |
| Structured decisions | Jev (optional) → `policy_gate` | Jev returns choices/scores only; rights are never a model decision |
| Optional long-video clipping | SupoClip as a separate remote service | AGPL-3.0; not imported into this codebase |

The research API authenticates with `X-Research-Token` (constant-time compare), not
`osk_` keys, so it works in self-host mode. With no token set, it answers 503.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/topic-jobs` | Validated, idempotent job (`Idempotency-Key` header or `idempotency_key`; same body on the same UTC day dedupes). 409 on key reuse with a different body, 429 above `RESEARCH_MAX_JOBS_PER_HOUR` |
| GET | `/topic-jobs/{id}` | Job + source counts by rights |
| POST | `/topic-jobs/{id}/sources` | 1-50 metadata records. URLs canonicalised and never fetched; NULL metrics get a reason; repeats become new metric snapshots |
| GET | `/topic-jobs/{id}/sources` | Sources with latest metrics and features |
| POST | `/sources/{id}/media-analysis` | Words/cuts/captions measured on the remote worker. **403 for `metadata_only`** |
| POST | `/topic-jobs/{id}/classify` | Hook archetype + topic fit (Jev under budget, else rules) |
| GET | `/topic-jobs/{id}/cohort` | Medians, quartiles, bootstrap CI, n / n_missing, caveats; synthetic records excluded by default |
| GET | `/spend` | Today's reserved + settled spend vs cap |
| POST/GET | `/channels` | Channel registry for accounts you created yourself. Requires `owner_attestation`; stores no tokens (Upload-Post holds them) |

## What was verified in the source repos (2026-11-01)

- **AI Shorts already has an API**: `POST /api/saasshorts/analyze` (Gemini scripts) →
  `POST /api/saasshorts/generate` (`script` dict, `X-Fal-Key`, `X-ElevenLabs-Key`,
  `video_mode` `lowcost`|`premium`) → `GET /api/saasshorts/status/{id}`. No new render
  endpoint is needed: Phase 3 maps approved scripts to that `script` shape (`segments[]` with
  `type/start/end/narration/visual/broll_prompt/subtitle_text`, `full_narration`, `caption`).
- **Render cost** (OpenShorts' own estimate in `saasshorts.generate_full_video`, lowcost mode,
  about 20 s): actor image $0.05 + Hailuo $0.19 + VEED lipsync $0.20 + 2 B-roll stills $0.10 +
  voice ≈ $0.01 → **about $0.55 per video**. Premium mode (Kling) costs more and scales with duration.
- **`/api/process`** is for owned/authorised footage (`acknowledged: true`, signed webhook). It is
  not the topic-to-original route.
- **Official n8n examples**: `openshorts-content-machine.json` gives reusable nodes for Telegram
  approval, the slot picker, `POST /api/social/post` scheduling and the weekly digest. Its README
  verifies the webhook HMAC over `JSON.stringify(body)`, which can differ from the raw bytes the
  server signed. Phase 4 must verify the raw body.
- **instagram-skills** (MIT): runtime deps are only `requests` and `python-dotenv`. It talks to
  Apify (read), Publora (post) and Pixfaro (images). Its "diy" backend runs a user-configured
  command. The hook taxonomy here is written fresh for spoken Reels; nothing from that package
  is installed or executed.
- **Jev**: request/response shape checked against the official docs; input tokens are billed;
  no public price, so `JEV_COST_PER_1K_INPUT_USD` must be set to your quote.
- **Account generators**: neither repository is used or run.

## Run locally (no Docker, no network)

```powershell
cd "H:\My Drive\PC\Clip_Factory_Claude_n8n_Kit\openshorts"
& "$HOME\.venvs\clip-factory\Scripts\python.exe" -m pytest tests/test_research_analysis.py tests/test_research_api.py tests/test_research_integration.py -q
& "$HOME\.venvs\clip-factory\Scripts\python.exe" -m research.dry_run
& "$HOME\.venvs\clip-factory\Scripts\python.exe" -m research.db --print-ddl
```

## Not built yet

- Phase 2: a worker that fills `media-analysis` from an owned upload (faster-whisper words +
  `scene_detection` cuts) on the remote host, plus the Patterns page in `dashboard/`.
- Phase 3: Script Studio (original drafts in the SaaSShorts `script` shape, `originality.py`
  gate, fact sources, approval state), then one render through `/api/saasshorts/generate`
  and a Drive archive with a separate OAuth client.
- Phase 4: the content machine adapted to the approved-script queue, raw-body HMAC webhook
  verification, and one approved post.
