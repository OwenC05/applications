# Local hybrid-RAG application copilot

**Status:** Sequential Architect and Critic reviews approved cycle2. Planned, not implemented.

**Verification:** [verification specification](local-rag-tests.md)

## Outcome and scope

Each friend runs an independent local Docker installation for technology and finance applications. They deliberately import documents, confirm personal evidence, add a role, research the employer, and generate grounded answers with inspectable citations. They review wording and submit externally themselves.

**Required stack:** Python/FastAPI, Chroma dense vectors, local BM25, reciprocal-rank fusion (RRF), local cross-encoder reranking, Docker Compose. SQLite is canonical; retrieval indexes are rebuildable projections. Reuse the existing browser frontend.

Preserve resumable/skippable approximately hour-long onboarding, explicit confirmation of new facts, exact-role/company research, per-profile cloud opt-in, own server-side key, and exact-version human review. Local deployment does not mean all inference is local, encrypted storage, or zero provider retention.

**MVP includes:** TXT/Markdown/text-PDF ingestion; confirmed evidence bank; grounded document Q&A; per-question hybrid retrieval; original-source employer research; existing bounded planning/drafting/critique workflow; evidence inspector; durable jobs; backups; evaluations.

**Excluded:** hosted accounts, shared friend server, OCR, encrypted PDFs, DOCX, filesystem crawling, Trackr scraping, automatic submissions, universal autofill, fine-tuning, Kubernetes and exported telemetry. Opportunity entry remains manual.

## Repository evidence

Existing `application-copilot/src/server.mjs` supplies loopback/token/Host/Origin controls; `src/store.mjs` provides explicit fact confirmation, optimistic revisions and exact review history. `src/context.mjs` is verified-only substring retrieval, not hybrid RAG. `src/ai.mjs` has bounded critique/rewrite, but research excerpts come from model-produced text rather than original downloaded pages.

`public/*`, `API.md`, `DESIGN.md`, `README.md` and `test/*.test.mjs` provide interface and behavioral specifications. Historical 73 synthetic tests do not verify Python, Docker or this retrieval architecture. No private documents or credentials were inspected.

## RALPLAN-DR / ADR-001

**Principles**

1. Truth and user-controlled evidence precede persuasion.
2. Canonical data is authoritative; indexes are disposable.
3. Local ownership and minimum disclosure are defaults.
4. Partial operations and unsupported evidence fail closed.
5. Quality and operability require measurements, not technology labels.

**Top drivers:** grounded employer-specific answers; independent friend usability; demonstrable provenance/recovery without unnecessary services.

**Option A — selected:** FastAPI, SQLite state/job ledger, one durable Python worker, Chroma, persisted bm25s generations. Fewer services and straightforward local ownership; requires carefully tested leasing/recovery and limits parallel throughput.

**Option B:** identical mandated retrieval stack with Redis/Celery workers. Better established distributed dispatch/concurrency; additional services, persistence and recovery complexity. It still needs canonical/index consistency and idempotent provider handling. Reconsider only after measured demand exceeds one worker.

Choose A for this private single-installation workload. Keep lexical-only retrieval as an evaluation baseline, not a replacement for the required hybrid pipeline. SQLite volumes must be local, not a shared network filesystem.

## Data, ingestion and citations

SQLite stores profiles, consent, interviews, fact states, applications, source registry, canonical text/chunks, immutable draft/review records, manifests, jobs and usage reservations. Private blob volumes retain original uploaded/fetched bytes and hashes.

Separate corpora:

- **Raw personal documents:** available to explicitly selected document Q&A.
- **Confirmed personal facts:** eligible for application generation.
- **Employer/role sources:** scoped to the relevant profile/application.

A CV upload never confirms its contents. Extracted claims become pending proposals. Confirmation creates an immutable **fact version** containing owner, canonical confirmed text, text hash, confirmation timestamp and provenance kind: manual, interview, document-derived or legacy user-confirmed. Application indexes contain this confirmed wording, never adjacent raw-document text, raw interview transcripts or historical generated answers.

Original document spans are optional provenance links, not automatic proof of corrected wording. Editing a proposal creates a newly confirmed fact version; retain an old excerpt only as “origin of proposal” unless it genuinely supports the corrected statement. Manually entered and interview facts cite their canonical confirmed records. Legacy verified memories retain labelled legacy user-confirmed provenance; their import neither retrofits document support nor renews historical review eligibility.

Accept explicit UTF-8 TXT/Markdown and text-based PDFs using pypdf. Reject encrypted/scanned/no-text PDFs, malformed input and unsupported formats with actionable explanations. Initial configurable limits: 10 MB/upload, 100 pages, one million extracted characters and 30-second parser deadline. Use bounded worker parsing and extraction preview; never execute embedded content or crawl directories.

Preserve immutable canonical text per PDF page, or TXT/Markdown lines/sections, with stable character offsets. Store parser version and original-byte/canonical hashes. Parser changes create new versions; do not reconstruct citation text by decoding embedding tokens.

Each citation resolves server-side to an authorized immutable **fact version or source version**, its canonical hash, exact span and excerpt. Document citations additionally retain page/line location. Reject fabricated, foreign, revoked, stale or altered references. The viewer highlights the canonical text snapshot and can open the original PDF at its physical page; PDF geometric highlighting is not promised. Application copy omits internal citation markup; a separate evidence report preserves provenance.

**Citation integrity is distinct from semantic support.** Persist a support ledger bound to the exact draft ID and final text hash. Each factual claim records answer/cover-letter span, claim kind, supporting fact/source-version IDs, integrity result and support state: supported, unsupported, contradicted or needs_confirmation. All personal and employer factual assertions require coverage; personal assertions use confirmed fact versions, employer assertions current source versions, and inferences are explicitly labelled.

Reuse the bounded critique to assess support; this is not formal entailment or external certification. Relevance scores are not entailment probabilities, and valid citations are not a verified-truth badge. Unsupported, contradicted, unassessed or needs_confirmation factual claims block review even when reference IDs are valid. Invalid citations, required factual gaps and format failures also block review; human review remains mandatory. Style suggestions remain advisory. Prose edits invalidate the affected ledger; source changes invalidate dependent entries. A dedicated reassessment operation validates the exact saved text and references, performs at most one consented semantic-assessment call, and commits only against unchanged revisions. No rewrite occurs in reassessment. Without cloud access, editing/copying remains available but newly edited prose cannot receive a fresh assessed/reviewed state. New personal facts require explicit confirmation first; neither user approval nor reassessment promotes facts automatically.

**Document Q&A:** Without a key or consent, provide local retrieval, exact quotations and source inspection only—not generated offline answers. Cloud Q&A requires an explicit request disclosing transmission of spans from user-selected documents. Retrieve only from that selection; check ownership, consent and source revisions before each transmission and final save. No hidden cloud embedding fallback.

Cloud Q&A has at most two calls: answer, then support assessment of that exact answer against supplied spans. No automatic rewrite. Unsupported/contradictory content produces an explicit insufficient-evidence response with source quotations, not a supported-answer badge. Store the text-bound support ledger. Raw-source answers remain “document states,” not confirmed applicant evidence.

## Hybrid retrieval and index consistency

Candidate local models: `sentence-transformers/all-MiniLM-L6-v2` embeddings and `cross-encoder/ms-marco-MiniLM-L6-v2` reranker. Pin exact model commits, tokenizer/config hashes, dependencies and licenses after compatibility testing. English is the initial supported language.

Embedding chunks must respect the actual 256-wordpiece model limit and preserve canonical offsets. Explicitly bound reranker query/passage pairs and record truncation. No hidden cloud embedding fallback or runtime model download.

For each question, combine its wording with role criteria and sourced employer priorities. Query scoped Chroma dense vectors and scoped bm25s against the **same active generation**, fuse rankings, deduplicate and rerank. Initial experiment: 30 candidates/branch, RRF k=60, 40 fused candidates, 6–8 selected chunks within context budget. Tune these hypotheses against frozen data.

Build generation G in staging: eligible canonical snapshot → chunks → dense and sparse artifacts → consistency checks → atomic SQLite active-manifest switch. Manifest binds owner, corpus, source revisions, model identity and both indexes. Queries read one immutable manifest. Missing/mismatched branch blocks retrieval rather than silently degrading. Recheck eligibility after retrieval and before generation; superseded/deleted/unconfirmed material cannot reappear through an older generation.

Expose two distinct actions. **Remove reusable fact** revokes its versions, application retrieval and dependent prose/support; retained raw documents remain searchable in document Q&A, clearly disclosed before confirmation. **Delete source and derived data** removes source bytes/text, proposals, chunks, indexes and affected outputs; linked fact versions are revoked by default. Preserving one requires a separate explicit confirmation as a new standalone fact, without deleted-source citations—never automatic conversion.

Both actions transactionally invalidate access, cancel dependent work and suppress queued re-extraction/reconfirmation of the revoked source/proposal versions. Durable cleanup verifies all derived stores and caches; display pending until complete. Content-free tombstones prevent version resurrection, not recognition of every future paraphrase or deliberate re-import. Profile deletion covers all app-held raw and derived content. External backups, exports and provider copies remain disclosed exceptions.

## Original-source employer research

Discovery may use consented provider search, but complete research requires retained original company **and exact-role** source snapshots. Existing model-text excerpts cannot be migrated into original-source citations. Inaccessible/ambiguous sources remain incomplete and block “researched tailored” generation. User-provided text retains that provenance, not a fetched-public-source badge.

Add a narrowly scoped acquisition service: user-confirmed official domains and configured relevant ATS hosts only; HTTPS; exact hostname rules; no URL credentials; validated redirects and DNS/connect destinations; block private, loopback, link-local, metadata and rebinding targets. Initial limits: 10 pages/run, 2 MB/page, 15 seconds/fetch and bounded total deadline. Accept bounded HTML/text/PDF parsing, not browser script execution, credentials, CAPTCHA/paywall bypass or Trackr scraping. Respect published access restrictions.

Retain final URL, retrieval time, original snapshot/hash and canonical spans. Priorities inferred from sources remain labelled inferences. Preserve seven-day freshness and immediate role-input invalidation. Research receives no personal brain.

## Durable execution and local deployment

Port existing domain routes and envelopes. Add explicit job creation/status/cancel endpoints for long operations; update the frontend for asynchronous responses rather than silently breaking old full-profile response contracts. Preserve Host/Origin/token/CSP protections; profile selection is not authenticated multi-user security.

SQLite jobs contain captured revisions, idempotency key, lease/heartbeat, bounded attempt limit and a monotonically increasing fencing token. **Every job-owned canonical write**, including stage results, support ledgers, final drafts and active-index publication, transactionally checks the current fencing token and relevant revisions. Expired workers cannot publish after another worker claims the job.

Before each cloud transmission, persist its attempt and usage reservation. Recovery distinguishes definitely unsent work, committed completed work and indeterminate attempts that may have transmitted. A prepared attempt interrupted before apparent send is conservatively indeterminate unless non-transmission is provable. Lease expiry never authorizes retransmission or releases uncertain reservations. Pause indeterminate work; only an explicit retry acknowledging possible duplicate cost creates a new recorded attempt. No exactly-once provider guarantee is claimed.

Preserve consent/ownership/version checkpoints before every transmission and final save and the existing plan → draft → critique → at most one rewrite → fresh final critique. Revocation stops subsequent calls, not an already transmitted request. Retry only demonstrably safe stages with bounded backoff/attempts; never blindly retry ambiguous paid requests or auth/quota failures. Previous drafts remain intact on failure. Cloud defaults disabled; request/token ceilings and usage reservations remain conservative. Distinguish estimated from reported costs; do not promise exact monetary caps without validated pricing/billing controls.

Compose contains API, worker and private Chroma. Publish only API at `127.0.0.1:3000`; Chroma has no host port. Use persistent volumes, non-root processes, resource limits, health/readiness and mounted secrets restricted to necessary services. Exclude `.env`, personal data, indexes and backups from build context and images.

Model setup is explicit, pinned and cached. Docker prerequisites, model download and key setup precede any “one-command startup” claim. Docker Desktop’s own memory minimum does not establish RAG requirements. A 16 GB host/CPU-first baseline is a test candidate, not a measured guarantee.

Back up SQLite consistently with its referenced blobs; rebuild derived indexes after restore. Restore into an empty instance, validate integrity and require explicit cloud re-enablement. Explain plaintext local storage and OS/drive protection honestly.

## Delivery phases and resources

All implementation paths below are proposed relative to `application-copilot/`.

1. **Python parity and migration:** `backend/pyproject.toml`, lockfile, `backend/copilot/{api,schemas,store,migrations,interview}.py`, `backend/tests/`. Port existing behaviors. Add explicit dry-run/non-destructive legacy JSON import with backup, IDs/revisions/history preservation and collision rejection. Never overwrite legacy state; old research becomes provenance-incomplete.
2. **Ingestion and retrieval:** `documents.py`, `citations.py`, `retrieval/{dense,lexical,fusion,rerank,indexing}.py`, pinned model manifest and fixtures. Gate on confirmed-only application retrieval, coherent generations and citation/deletion tests.
3. **Research and generation:** `research.py`, `provider.py`, `jobs.py`, `worker.py`, `quality.py`. Gate on original-source acquisition, support/abstention, bounded jobs/budgets and consent checks.
4. **Friend delivery:** update `public/*`, `API.md`, `DESIGN.md`, `README.md`; add `Dockerfile`, `.dockerignore`, `compose.yaml` and backup/restore/smoke scripts. Gate on clean setup, complete application workflow and recovery.
5. **Release evidence:** `eval/{dataset.jsonl,run.py,report.md}`; retrieval ablations, adversarial support evaluation, tech/finance human review and CPU measurements.

Future ownership: backend executor phases 1/3; retrieval executor phase 2 after schema freeze; frontend executor after API freeze; root integrates infrastructure/docs; independent verifier checks evidence. No implementation starts during this planning run.

## Deliberate pre-mortem

1. **Convincing false applications:** citations resolve but do not support claims; raw CV statements become facts. Mitigate corpus separation, span validation, support/abstention tests and human review.
2. **Private/deleted data returns after crash:** mixed indexes or late worker commits. Mitigate tombstones, immutable manifest, final authorization and crash/deletion fault injection.
3. **Friends cannot operate it:** downloads, resource pressure or migration failure. Mitigate bounded formats, explicit setup checks, non-destructive import, tested restore and two-friend installation trials.

## References and stopping rule

Official/upstream constraints: [Chroma Docker](https://docs.trychroma.com/guides/deploy/docker), [Cloud-only native hybrid API](https://docs.trychroma.com/cloud/search-api/overview), [bm25s](https://github.com/xhluca/bm25s), [embedding model](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), [reranker](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2), [RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf), [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html), [FastAPI background tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/), [Compose](https://docs.docker.com/reference/compose-file/services/), [Windows prerequisites](https://docs.docker.com/desktop/setup/install/windows-install/).

Sequential Architect and Critic reviews approved this plan. New implementation has not started; dependency, model, Docker, performance and live-quality checks remain required.
