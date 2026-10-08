# Evidence milestone — verified implementation, 8 October 2026

## Delivered, separately from the existing Node app

- Python/FastAPI evidence API and browser workbench, SQLite canonical records.
- Deliberate TXT/MD/text-PDF import with bounded Linux PDF subprocess; immutable confirmed facts, corrections and canonical Unicode citations.
- Actual local MiniLM dense embeddings, Chroma, persisted BM25, deterministic RRF and local cross-encoder reranking. Selected-source filtering precedes both ranking branches.
- Fail-closed model/index coherence, snapshot revision publication/rechecks, serialized CLI builds, deletion tickets and restart cleanup.
- CPU dependency lock, immutable model revisions, explicit download/cache setup, non-root Docker app and private Chroma Compose scaffold.

The Node sources, UI, tests and package scripts are unchanged. There is no legacy data import, integrated Python application drafting or automatic submission.

## Fresh evidence

| Check | Observed result |
|---|---|
| Full Python suite, including explicit real-model and HTTP opt-ins | **68 passed**, no skipped tests; one upstream TestClient deprecation warning |
| Node regressions | **73 passed** |
| Ruff (backend and eval) | Passed |
| Python module compilation / JavaScript syntax | Passed |
| Evaluation metric unit tests | **3 passed** |
| Chroma transport | Real loopback HTTP server, pinned installed distribution1.5.9; heartbeat, embeddings, source filter, BM25, citation and collection deletion passed. Server version endpoint reports1.0.0; this is not represented as release-version proof. |
| Browser | Synthetic tech+finance profile; TXT import/preview; explicit confirmation; real fact/document retrieval; canonical highlight; fact revoke with raw source retained after rebuild; source/profile deletion and empty restart state. |
| Browser isolation | Delayed profile-A response did not render after switching to profile B. This check used an intercepted synthetic response, not model-quality evidence. |
| Browser Unicode/mobile | Actual `🚀 Python evidence` citation renders exact canonical text; 390px viewport had no horizontal overflow. Console clean after final reload. |
| Cleanup aftermath | Fresh Store instance:0 profiles,0 pending tickets,0 owned blob files; HTTP Chroma:0 remaining test collections. |
| Static delivery / publication | YAML parsed; loopback-only app port, no Chroma host port, pinned image digests, non-root app, build-context allowlist, no CUDA dependencies or new credential/cache files. |
| Live Docker | **Not run: Docker is unavailable on this host.** Static checks are not Compose/image/daemon verification. |

Reproducible commands are in the root README. Test data were synthetic and independent of the user's existing `.env`, `.data`, CVs and coursework. Paid/cloud-provider calls: zero. Public model/dependency downloads were explicit setup work.

### Pre-publication recheck

An independent verifier reran the bounded local checks on 8 October before publication: **73 Node tests**, **66 Python tests with 2 explicit real-model/HTTP skips**, **3 evaluation metric tests**, Ruff, Python compilation and workbench JavaScript syntax all passed. No dependencies were synced/downloaded and no source files changed. The earlier 68-pass model/HTTP and browser results above are historical evidence, not newly reproduced in this recheck. Docker remains unavailable; no fresh Docker, browser, standalone type-checker or vulnerability-audit result is asserted.

## Independent review and fixes

Sequential native Planner → Architect → Critic approved the bounded slice plan; a user-authorized local handoff was recorded before implementation. OMX Ralplan/Ultragoal runtime could not bind the session; native executors were used instead. No canonical runtime/goal completion or host-security authority is claimed.

Independent code review initially requested changes. Fixed and regression-tested:

1. Explicit confirmation now rejects coercible strings/numbers (`StrictBool`).
2. Failed build attempts dense and sparse cleanup independently; failures are visible/recoverable.
3. Model setup and API use the same directory; local CLI commands have an explicit working directory.
4. Abrupt pre-commit upload crashes reconcile untracked UUID blobs under transactional exclusion, preserving other owners and active writers.
5. Failed service recovery is never cached as ready.
6. Browser citation slices use Unicode code points, not UTF-16 offsets.

Independent delta code review approved these repairs (33 targeted tests passed). Architecture review found no remaining blocker in the bounded quote-only milestone, with **WATCH for release qualification**. Dedicated Chroma is mandatory; shared instances are not supported. Cleanup removed an unused citation pass-through and duplicate text parsing; fail-closed and pending-deletion behavior were preserved.

## Retrieval evaluation — useful failure, not a quality certificate

Frozen v2:10 synthetic facts,12 queries, including duplicates, conflicting quotations, lexical distractors and2 unanswerables. Dataset SHA256: `a408c56a4912ae923bb69694fcc0836c4dfabb6fae29fc423ce5eeb57d8d5d61`.

All three branches measured answerable Recall@8=1.0, nDCG@8≈0.9693, MRR@8=0.95 and top1=0.90. They **all ranked a snake distractor first on the negated software-not-wildlife question**; there was no measured hybrid advantage. Unanswerables returned evidence, not semantic abstention. Labels and parameters were not changed to conceal these outcomes.

Tiny warm p50/p95≈1.30/1.99seconds over10 short chunks; cold build≈17.82seconds. These are not the1000chunk performance gate. Machine: Python3.12.15, Linux x86-64 CPU,7.6GiB total RAM. Full results: [`eval/latest-smoke.json`](../../eval/latest-smoke.json); interpretation: [`eval/README.md`](../../eval/README.md).

## Remaining slices and release gates

1. Port profile/interview/application domain APIs and explicitly migrate legacy data without overwriting it; integrate the evidence workbench into one experience.
2. Add original company/exact-role snapshots with safe acquisition; retrieve employer priorities separately from confirmed applicant facts.
3. Integrate bounded drafting, exact-text support assessment, abstention and human review; durable worker fencing, consent and indeterminate paid-call handling.
4. Qualify Docker startup/persistence/resource limits, restore/backup,1000chunk CPU performance, broader held-out/human evaluations and independent friend installs.

The current milestone is **not production-ready** and cannot establish hiring quality. Storage/deletion are application-level, not forensic media erasure. No standalone type-checker, dependency vulnerability audit or complete hostile browser/PDF fault matrix was run.
