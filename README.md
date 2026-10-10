# Application Copilot

A private, local-first internship application workspace for **technology and finance**. Includes the preserved dependency-free Node.js MVP and a separate Python/FastAPI evidence workbench.

**Snapshot status:** Node onboarding/research/drafting and the Python evidence workbench are implemented. Python also has reviewed profile/interview/proposal/application data APIs, an explicit legacy importer and an isolated workspace preview. Durable workers handle indexing and bounded static company/role research with original-source inspection through the API. The workbench has Chroma dense + BM25 retrieval, reranking and Docker scaffolding; Docker runtime is not verified. These components are not yet an integrated researched-drafting pipeline. Grounded application drafting and later supervised browser filling/submission are still being built. See [implementation checkpoints](docs/implementation-status.md) and [the reviewed plan](docs/plans/README.md).

## Start

Requires Node.js 24 or newer. From this directory:

```sh
npm start
```

Open **http://localhost:3000**. Create your own blank profile or use **a separate, clearly synthetic demo**. Friends can run their own copy with their own local data. Selecting profiles on a shared machine is a convenience, not authenticated account security.

## Enable real company research and AI drafting

Copy `.env.example` to `.env`, set your own `OPENAI_API_KEY` locally, optionally set `OPENAI_MODEL`, and restart the server. Never paste the key into chat, put it in a browser field, or commit it. The documented default is `gpt-6-astra`; your account needs access to the configured API model/tools. In each profile's Settings, explicitly enable cloud processing. Requests may incur provider usage charges; no cloud calls happen without both consent and configured credentials.

Research uses actual public employer/role retrieval, restricted to the company domain you supply and a relevant trusted ATS domain when present. It retains consulted source URLs, supporting excerpts, timestamps and separately labelled inferred priorities. It requires company AND exact-role coverage before cloud drafting. It then combines relevant verified personal memories with that research and actual application questions. These checks are not semantic proof: inspect sources and every generated claim. We cannot know a hiring manager's private thoughts.

Cloud drafting now plans the strongest supported example/angle for each question, drafts, and independently critiques the exact wording against the original evidence and research. If issues remain, it rewrites once and critiques that final version again: **3–5 model calls**, with added latency and usage. The advisory quality panel identifies weak specificity, role fit, unsupported claims, unanswered questions and format issues. Factual questions stay direct; gaps require your confirmation, not invented achievements. An editorial pass is not factual certification or a hiring-success score. Editing the prose or changing its underlying evidence makes the assessment stale.

**Offline mode never pretends to research or personalize.** It provides deep onboarding, memory review, role tracking and explicit generic draft templates with placeholders. Replace placeholders yourself, or configure cloud processing. The live provider path is implemented and tested using synthetic injected responses; no live model request was made during development because no user key was supplied.

## Use the workspace

1. **Interview:** 48 substantive common/tech/finance questions (sector-filtered). Around 45–60 minutes is a target, not a compulsory timer. Save, skip, resume or finish early. Request adaptive follow-ups when useful. Saved answers propose pending evidence; unanswered questions never become invented facts.
2. **Brain:** confirm/reject pending memories and inspect provenance. Add verified projects, behavioral stories, achievements and motivations. New or changed facts always need explicit confirmation, even when they appeared in a reviewed application. Correcting a fact supersedes it and stales derived drafts.
3. **Opportunities:** add an actual employer/role, official company URL, job description, and all application questions with word/character limits. Both sectors work equally. The first version uses manual listings; no claim of exhaustive coverage.
4. **Research:** request real company AND role research. Missing/unsourced/stale evidence blocks tailored cloud drafting instead of generating a confident generic answer.
5. **Applications:** generate, inspect the question plan and editorial issues alongside evidence/gaps, edit, copy or download; review the exact saved version. Only you submit on the employer site. **Mark submitted** records your explicit action; it does not submit anything or observe employer confirmations.
6. **Learning:** approved wording/history is logged in an immutable application snapshot. Answer content can become a pending memory candidate, not an automatically verified fact. Historical prose is never sent to the model as factual evidence. Confirm and refine useful new memories before future reuse.

## Data and privacy

- Brain/interviews/research/drafts/history persist in `.data/state.json` with atomic writes and restricted permissions where the filesystem supports them. Windows-mounted filesystem permissions may not enforce Unix modes. **Local storage is plaintext, not encrypted**; protect the OS account/drive.
- API key is server environment only. Selected profile ID may be stored in browser localStorage; personal brain data is not.
- Server binds to loopback, validates Host/Origin, requires a boot-session token and supplies a restrictive CSP. It never executes webpage/model instructions or fetches arbitrary job URLs itself.
- Cloud research does not receive personal memory. Drafting receives relevant verified memories; requested interview follow-ups receive your answer and relevant verified evidence. Consent belongs to each profile.
- Persisted consent and source versions are checked before each cloud transmission and again before saving. Revoking consent stops subsequent stages; it cannot recall an already transmitted request. A failed quality stage leaves the previous saved draft untouched.
- `store:false` requests are used, but this is **not zero provider retention**; applicable abuse-monitoring retention may still exist. Read the provider's [data controls](https://developers.openai.com/api/docs/guides/your-data).
- **Forget memory** removes that memory and linked interview source; conservatively clears ALL derived draft/history content for that profile so deleted facts cannot hide in human-edited prose. Other verified memories remain. Inspect the confirmation before forgetting.
- **Delete profile** removes all its app-held content. Existing downloads, OS backups and already transmitted provider/employer data are outside app-local deletion. Exports contain personal data; store them privately.
- One server owns a data directory. A second instance fails safely. An unclean crash can leave a writer lock; do not blindly remove it while a server might be running. Its recorded owner must be confirmed dead before a bounded recovery. Normal Ctrl+C shutdown releases it.

## Development and verification

```sh
npm test
npm run check
```

Tests use synthetic profiles, injected provider responses and temporary local HTTP servers. `--test-isolation=none` ensures individual assertions run in this environment. Syntax checks cover source/frontend/test modules; no external linter or TypeScript compiler is installed. Current architecture contracts: [API.md](API.md), [DESIGN.md](DESIGN.md).

## Existing Node MVP limits

- **No browser autofill or automatic submissions yet.** Supported-platform browser filling is the next phase.
- **GitHub listing import is not enabled yet.** A conservative normalizer exists, but the live upstream schema/licensing/provenance needs validation before connecting it. Manual job entry supports tech and finance now. Trackr is inspiration only; its [terms](https://the-trackr.com/terms-of-use/) restrict automated extraction and applications.
- No hosted accounts, encrypted datastore or fine-tuning. The Node MVP has no PDF CV parsing or vector database; the separate Python workbench below adds deliberate document parsing and retrieval. Existing CV/course files were not read or changed.
- Model output can still be wrong. Source/evidence ID checks help review but do not prove every sentence is factual. Questions or gaps require you, not guesses.

## Official implementation references

[Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [documented model](https://developers.openai.com/api/docs/models/gpt-6-astra).

The editorial rubric draws on public [Oxford application-form guidance](https://www.ox.ac.uk/careers/careers-guidance/job-search-and-applications/writing-applications/application-forms), [Microsoft hiring guidance](https://careers.microsoft.com/v2/global/en/hiring-tips) and [JPMorganChase's hiring guidance](https://www.jpmorganchase.com/careers/how-we-hire). Applying interview guidance to written answers is our design inference, not employer endorsement; the actual employer's published application instructions take precedence.

## New: Python evidence milestone (separate workbench)

The existing Node application above is preserved. This additive milestone is **not yet integrated with application drafting**. It provides local document import, explicit fact confirmation, hybrid retrieval and citation inspection on **port 3001**, with a separate SQLite data root. It makes no cloud calls or generated-answer/hiring-quality claims.

The additive `/api/workspace` data services share that same Python/SQLite profile
and confirmed-fact authority: 48-question onboarding, pending confirmations,
application inputs, immutable feedback history and purpose-specific consent.
Typed personal information stays outside story retrieval. An explicit selected
single-profile Node export can be dry-run/imported with exact-byte confirmation;
Node files are never discovered or modified. Old cloud consent and review badges
are not carried forward. These routes are documented in [API.md](API.md); the
existing evidence page remains the default until the unified UI is qualified.

### Python workspace preview

With the Python server running, open
`http://127.0.0.1:3001/ui/workspace/index.html` (use your configured port).
This separate preview connects interview, explicit pending-fact confirmation,
tech/finance opportunity inputs, feedback history, selected document import,
hybrid quotation retrieval and citation inspection to the canonical Python data.
Settings include separate typed personal values, explicit purpose consent,
selected legacy-import dry runs, export and deletion status. No private documents
are discovered automatically, and generated/history text never becomes a fact
without confirmation.

Application activity offers explicit job/status/cancellation refresh, profile-wide
usage and stored static-research history/source inspection. Refreshing these panes
preserves unsaved application inputs; historical completeness is not current
eligibility, and stored hashes are not semantic support. Cloud/research launch,
drafting, paid retry, application review and browser actions are **not available
in this preview**. Existing question text/order cannot yet be
changed in its line editor: it refuses changes rather than reassigning IDs or
constraints. Saved-state JSON export is not a qualified full backup. Independent
review and synthetic actual-browser flows cover this component, not full S6,
accessibility or release qualification. The default evidence page and Node app
are unchanged.

### Docker setup

Prerequisites: Docker Engine/Desktop with Compose, Linux containers and enough CPU/RAM for two local text models. The current development host has 7.6 GiB RAM; a small real-model smoke is not a capacity benchmark. Docker deployment is **not runtime-verified here** because Docker is unavailable.

```sh
docker compose build
# Explicit model download (~180 MB of weights; additional pinned dependencies in image).
# Runtime stays offline. This setup command alone enables HF network access.
docker compose run --rm -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 app python -m copilot.models setup
docker compose up -d --wait
```

Open `http://127.0.0.1:3001`. Create a profile, deliberately upload TXT/Markdown/text-based PDFs, inspect text, and explicitly confirm reusable facts. Copy the profile ID from the terminal commands shown in the workbench. Prepare each index after evidence changes:

```sh
docker compose exec app python -m copilot index --profile PROFILE_ID --corpus facts
docker compose exec app python -m copilot index --profile PROFILE_ID --corpus documents
# Resume an interrupted deletion; never delete volumes as a substitute for this operation.
docker compose exec app python -m copilot cleanup
```

Only the workbench port is published, on loopback; Chroma and the background worker have no host ports. Named volumes retain evidence, models and Chroma; the worker mounts the model cache read-only. Health means the API is alive, **not** that models/indexes are prepared. Status deliberately does not scan the cache; indexing and queries validate readiness and both retrieval branches. Keep a single API process and one normal background worker, without development reload.

Indexing now uses durable fenced jobs. The commands above enqueue an index job and wait for its durable result, including when the background worker claims it. API clients can instead enqueue with `POST /api/workspace/profiles/PROFILE_ID/indexes` and inspect or cancel the returned job. The worker supports indexing and bounded static research; index routes do not launch research, and no drafting handler is enabled. Deletion revokes access immediately and returns cleanup pending for the worker to reconcile; a database outage or an unacknowledged remote write can keep cleanup pending.

Provider-attempt inspection and explicitly warned retry queuing are documented in
[API.md](API.md#durable-indexing-status-and-worker-outcomes). Retry may cause
duplicate charges: it preserves the original unknown outcome and reserved budget,
and requires unchanged inputs, current purpose consent and retry-family limits.
**Paid retry execution is unavailable**; queuing does not read a key or send a
request, and the worker rejects these jobs rather than running static research.

For a non-Docker installation, run the worker in another integrated terminal with the **same** data/model/Chroma settings as the API:

```sh
uv run --directory backend python -m copilot worker
# Diagnose at most one queued job plus registered cleanup; nonzero on failure or pending cleanup.
uv run --directory backend python -m copilot worker --once
```

The daemon emits bounded, content-free outcome changes to stderr. `--once` emits a versioned JSON job/heartbeat/recovery summary on stdout, not a success claim when recovery is pending. Cancellation prevents later authorized publication; it cannot recall an already dispatched operation. Upload parsing remains bounded synchronous work (PDF parsing uses a subprocess), outside broad API/storage locks, rather than a durable parser job.

### Python development / verification

Python **3.12** and [uv](https://docs.astral.sh/uv/) are required. The lock uses Linux x86-64 CPU Torch, not CUDA; macOS/Windows-native Python are not verified targets. Windows users use Linux Docker/WSL. No command imports the existing `.data` or reads the legacy `.env`.

```sh
uv sync --project backend --locked
uv run --project backend pytest backend/tests
uv run --project backend ruff check backend eval
uv run --directory backend python -m copilot.models setup
# Supply a private Chroma server on localhost:8000 (Compose uses hostname chroma internally).
uv run --directory backend python -m copilot serve
```

Optional explicit paths: `COPILOT_DATA_DIR` (default `evidence-data`), `COPILOT_MODEL_DIR` (default `<data>/models`), `COPILOT_CHROMA_HOST`, `COPILOT_CHROMA_PORT`, `COPILOT_PORT` (default 3001). Do not point the evidence data root at the existing Node `.data`. Models are pinned by immutable repository commits and their setup-time file hashes; normal loading is local-only and never trusts remote code.

**Evidence boundaries:** raw document search requires selected source IDs; application fact search indexes only explicitly confirmed canonical wording. A corrected/manual fact cites its own confirmed version, not an old PDF as proof. Citations check ownership, active version, exact Unicode span, hash and excerpt. Integrity is distinct from truth or semantic support; relevance/rerank scores do not certify either. The tool returns quotations, not offline generated answers.

**Deletion/privacy:** removing a fact retains its raw source; deleting a source revokes linked facts. Profile/source deletion immediately revokes app access, with exact owned external cleanup resumable through tickets. An absent Chroma collection or exited local producer does not prove an ambiguous remote write has stopped: completion requires its recorded write-envelope acknowledgment and cleanup checks; otherwise the ticket remains pending, potentially indefinitely. It does **not** promise forensic erasure from database freelists/WAL, service storage internals, host media/RAM, exports or external backups. Storage is plaintext; protect the OS/drive. Profiles are not authenticated friend accounts. Each friend should run an independent installation with their own data.

**Limits:** English, TXT/MD/text-PDF only; 10 MiB upload, 100 PDF pages, one million extracted characters, bounded Linux parser subprocess, 1,000 chunks per profile/corpus. No OCR, encrypted PDFs, filesystem crawl or automatic fact confirmation. Static company/role research now uses durable API/worker publication and source inspection, but only explicit URLs on confirmed hosts. The exact labelled identity adapter is not general employer/ATS support; unsupported layouts stay incomplete. There is no discovery/search agent or renderer. Python paid generation, employer indexing, support ledger, backup/restore qualification and browser submission automation remain pending.

Next slices and acceptance criteria: [evidence milestone](docs/plans/evidence-milestone.md), [tests](docs/plans/evidence-milestone-tests.md), and [current researched-application roadmap](docs/plans/README.md). Production release still needs live Docker/HTTP persistence/recovery, broader retrieval evaluation, supported-machine performance measurements and independent friend installation trials.

**Dedicated database requirement:** Do not connect this milestone to a shared Chroma instance. Recovery targets exact registered generation/manifest identities and preserves unknown artifacts rather than broadly sweeping `evidence_` collections. This conservative recovery is not shared-service isolation: a second installation must still use its own database/volumes. Installation-scoped namespaces are required before supporting shared Chroma services.

Real offline integration (opt-in, synthetic fixtures only):

```sh
COPILOT_REAL_MODEL_DIR=/path/to/models uv run --project backend pytest backend/tests -m real_models
# To additionally exercise an explicitly started loopback Chroma HTTP server:
COPILOT_REAL_MODEL_DIR=/path/to/models COPILOT_HTTP_SMOKE_PORT=8000 uv run --project backend pytest backend/tests -m real_models
PYTHONPATH=backend uv run --project backend python eval/run.py --models /path/to/models --output eval/latest-smoke.json
```

The evaluation is a small synthetic retrieval check, not semantic truth validation or hiring quality. Development currently emits an upstream Starlette/TestClient deprecation warning; tests still pass. Docker/resource-limit behavior and independent friend setup remain release checks.

Implementation evidence: [verified milestone and remaining gates](docs/plans/evidence-verification.md).
