"""Topic-to-channel research module (`/api/research`).

Stores public Reel metadata with provenance, analyses rights-cleared media
(word timing, pauses, shot cuts, narrative beats), classifies hooks with Jev or
a deterministic fallback, and summarises niche cohorts. Nothing here downloads
third-party media, calls a paid API without a budget reservation, or publishes.

Pure modules (`timing`, `beats`, `hook_taxonomy`, `originality`, `cohort`,
`urls`, `rights`) have no I/O. `db`/`repository` use SQLAlchemy Core so the
same code runs on PostgreSQL (production) and SQLite (tests, dry runs).
"""
