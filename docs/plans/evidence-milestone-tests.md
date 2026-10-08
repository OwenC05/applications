# Evidence milestone verification
Companion `.omx/plans/evidence-slices.md`. Synthetic data only; no reading/importing real .env/.data/documents, no paid/cloud-provider calls. Tests use temp data roots and owned ports. Baseline npm suite preserved.

## Unit gates
- Store immutable confirmation/version/supersession; ownership lookups, transaction rollback; origin span foreign/invalid rejected. Corrected “10%→40%” cites canonical confirmed version, never pretends old source proves it. Raw/pending/history cannot enter facts snapshot.
- Source UTF8/newline/Unicode canonicalization, half-open Unicode offsets, hash/excerpt equality, TXT line ranges/PDF physical-page mapping; malformed/encrypted/no-text/scanned/unsupported rejection; byte/page/character/deadline ceilings; cleanup failed upload. Synthetic parser memory-exhaustion and CPU/deadline fixtures prove subprocess termination and safe actionable errors; unsupported resource-limit platform explicitly rejects PDF parsing. Source preview/original attachment routes enforce ownership/revocation and safe attachment headers.
- Citation unknown/foreign/revoked/superseded ID, wrong hash, altered excerpt, invalid span all reject. Source origin remains proposal-origin label, not semantic support.
- Deterministic RRF and tie ordering, real bm25s persistence, dense dimensions, model fingerprint mismatch, tokenizer-safe chunks and bounded query/rerank truncation. Application chunks contain only canonical confirmed text.
- API Host/Origin/token restrictions; upload filename traversal, unsupported content; selected sources explicit/nonempty for document mode; sources belonging to another profile rejected before ranking; XSS strings rendered as text. Profile convenience selection not advertised authentication.

## Integration / fault injection
- Two profiles and two corpora with uniquely identifiable synthetic canaries. Chroma adapter + bm25s branches share exact ID-set/generation; selected source filters apply before branch top-k, so excluded high-ranked documents cannot starve selected results.
- Interrupt after dense stage, sparse stage, before manifest CAS and after publication; restart yields previous complete active generation or not-ready, never mixed branches. Missing sparse files, missing Chroma collection, wrong dimension/hash/model identity fail closed. Empty corpus defined empty response.
- Revoke/supersede/source-delete between snapshot, index publication, candidate generation and response. Old build publication fails revision CAS; response fails CONFLICT or excludes revoked records, citations no longer resolve. Lock plus revision guarantees tested without relying solely on lock.
- Cleanup interrupt at each derived store. Tombstone blocks reads immediately, ticket stays pending, restart resumes. Before complete, verify canonical queries cannot retrieve deleted text/chunks, owned blob/sparse files are absent, Chroma API reports collection absence, and process caches are evicted. Require content-free tombstones and UI/docs disclosure of plaintext storage and forensic limits; do not claim erasure of SQLite WAL/freelists, Chroma remnants, SSDs, host RAM or backup media. Source deletion revokes linked facts; fact-only revoke retains raw source and disclosure. Profile cleanup works after canonical profile tombstone. No automatic reconstruction from old staged generation.
- Explicit model download setup pins immutable revisions and files, verifies cache; missing/partial cache errors offline. Normal startup/search test rejects attempted network model downloads/cloud transport. Chroma localhost traffic allowed; no fake-network-deny test accidentally blocks required local database.
- Real smoke separately marked `real_models`: actual pinned local embedding + actual Chroma + actual bm25s + actual reranker indexes synthetic tech/finance evidence and resolves each returned citation. Persist exact versions, counts/timings and stage execution evidence; mocks alone do not satisfy this gate.

## Browser end-to-end
Use browser automation if available; otherwise report browser gate unrun (API tests not relabelled E2E). Start owned backend test port. Create tech+finance profile, upload synthetic TXT and text PDF, inspect preview, confirm edited fact, build each corpus, run fact query and explicitly selected document query, inspect exact canonical quote and PDF page, revoke fact, verify raw document remains with disclosure, delete source, restart, verify no retrieval/citation resurrection. Profile-switch delayed response cannot render previous profile data. Keyboard action/focus and loading/error states; no generated-answer or verified-truth badge.

## Evaluation / observability
Freeze small deterministic synthetic tech+finance dataset before tuning, including unanswerable, duplicate, contradictory and high-lexical-distractor queries. Record split and labelled expected IDs; compare dense-only, BM25-only, hybrid+rerank Recall@8/nDCG@8, candidate counts and p50/p95 timing. This milestone establishes evaluator, not parent 60-question/human-quality release gate. Retrieval cannot promise semantic abstention; no-hit only means no selected eligible evidence, scores never certify answerability. Any surprising degradation documented for next tuning rather than relabelling data.

Content-free structured stage/generation IDs, durations, chunk counts, model identity, readiness and safe failure codes only. Canary tests ensure source text, queries, filenames containing personal info, secrets and exception payloads do not enter default logs. Parser/model exception details sanitized. Performance report identifies hardware/corpus size/cold-vs-warm; no extrapolated16GB or5s guarantee.

## Commands and evidence record
Commands become valid after slice0; execute from repository root:
```
uv sync --project backend --locked
uv run --project backend pytest backend/tests
uv run --project backend ruff check backend eval
uv run --directory backend python -m copilot.models setup
uv run --project backend pytest backend/tests -m real_models
npm test
npm run check
docker compose config --quiet
docker compose build
docker compose up -d --wait
```
Root must document/create exact model CLI and smoke/eval invocation during integration; no placeholder command can count as verification. Record dependency/model hashes and actual exit statuses, not just commands. Docker absent: static compose/build-context/security inspection and YAML parsing provide bounded evidence only, not image/daemon health/persistence proof. Full friend installation, backup/restore, semantic support/human quality and1000chunk CPU benchmark remain release gates.

Completion evidence lists changed resources, Python/regression counts, real-model smoke pass or blocker, live Docker/browser outcomes or unrun gaps, and deferred parent milestones. Integrity/isolation/deletion or required retrieval-stage failures block accepted functional completion; environmental unrun gates must remain conspicuous rather than being silently waived.
