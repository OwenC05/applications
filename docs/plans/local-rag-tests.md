# Local hybrid-RAG verification contract

Companion: [product plan](local-rag-mvp.md). Use synthetic fixtures by default. Commands describe future implementation; no builds, downloads, private-document reads or paid calls are authorized by this plan.

## Unit gates

- Port ownership, pending confirmation, interview resume/skip, exact review, idempotency, stale writes and offline contracts from existing JS tests.
- Test TXT/MD/PDF extraction, quotas, encrypted/scanned/malformed rejection, page/line/span/hash fidelity and tokenizer limits.
- Prove raw/pending/rejected/superseded/history content cannot enter application evidence.
- Test dense dimensions, BM25 persistence, deterministic RRF, reranker bounds and same-generation enforcement.
- Reject invented/foreign/stale citation IDs, altered excerpts and unsupported claims with structurally valid citations.
- Validate every pipeline stage, exact question coverage, missing-fact gaps, factual answers without forced flattery and final critique of rewritten prose.
- Test SSRF suffix tricks, URL credentials, redirects, IPv4/IPv6 private destinations, DNS rebinding and streamed-size limits.

## Integration and failure gates

Use temporary SQLite and real pinned Chroma/BM25 synthetic indexes. Kill workers between dense write, sparse write and manifest publication; never expose a mixed generation. Mutate/delete facts or revoke consent during retrieval/generation; reject stale commits and stop later transmissions.

Restart leased jobs; prove bounded recovery and idempotency. Ambiguous provider timeout must not silently rebill. Concurrent requests must respect usage reservations. Offline/no-key/no-consent paths assert zero cloud calls using network-deny transports.

Deletion checks every derived store, cache and retained prose; restart must not resurrect content. Legacy import tests dry-run, corruption, collisions, repeat import and interruption, with unchanged original-file hash and no empty reset. Restore verifies hashes/counts, rebuilds indexes and disables cloud consent.

## Evaluation gates

Freeze at least 60 labelled English questions, evenly tech/finance, including 15 unanswerable and 10 adversarial/contradiction cases. Hold out examples before tuning. Compare lexical, dense and hybrid+rerank.

Initial targets: held-out Recall@8 ≥0.85; no >0.03 absolute regression against the best single retriever; unanswerable abstention ≥0.90; deterministic citation integrity 100%. Report nDCG@8, support precision, latency and failures. Failure requires tuning or release-blocker disclosure, not relabelling data.

Human-audit at least 40 generated factual claims; any invented qualification/result blocks release. Two reviewers blind-compare 12 tech/finance answers against existing lexical output for factuality, specificity, authenticity and question coverage; at least 8/12 new answers must be preferred or tied overall, with no critical unsupported claim. Report small-sample limits. Mocked responses cannot establish real editorial quality; live evaluation needs explicit credentials, consent and budget.

## End-to-end and operational gates

Browser flow: import → pending confirmation → interview resume → manual role → original-source research fixture → hybrid generation → citation inspection → edit/stale → exact review → externally-submitted marker → restart/export/delete. Check mobile/desktop, keyboard operation, safe source rendering and delayed-response profile isolation.

Docker checks: loopback-only published API, private Chroma, secret-free images/exports/logs, volume persistence, model/index readiness and actionable setup errors. Two friends independently follow README through first evidence query.

Benchmark a documented CPU-only machine, initially a 16 GB host candidate: 1,000 chunks, warm/cold runs, p50/p95 retrieval, indexing time and peak RAM. Target warm retrieval p95 ≤5 seconds excluding cloud; unmeasured until run. If unmet, tune within evaluation gates or publish the measured higher requirement.

Observability includes content-free run/stage IDs, versions, generation, counts, timings, retry/cancel outcomes and usage reservations. Canary tests prohibit keys, CV text and answer content in default logs/errors.

## Future verification commands

```sh
uv sync --project backend --locked
uv run --project backend pytest backend/tests
uv run --project backend ruff check backend
uv run --project backend mypy backend/copilot
npm run check
docker compose config --quiet
docker compose build
docker compose up -d --wait
uv run --project backend python scripts/smoke.py
uv run --project backend python eval/run.py --dataset eval/dataset.jsonl --offline
```

Executor must create these scripts/configurations, add browser/restore checks, and record exact dependency/model/image pins. Independent verifier reports results and remaining gaps. Isolation, deletion, migration, integrity or invented-qualification failures block release. Green mocked tests alone do not justify production-grade, live-quality or hiring-success claims.

## Cycle2 mandatory regressions

- **R1:** Manual, corrected interview, changed numerical and imported legacy facts resolve to their own canonical confirmed text/hash. Old document quotes cannot falsely support changed wording; adjacent pending text stays excluded.
- **R2:** “Contributed”→“led” and “10%”→“40%” edits invalidate support despite valid IDs. Review fails until exact-text reassessment succeeds against confirmed evidence; all factual spans have coverage. Confirmation, assessment and review remain distinct.
- **R3:** Kill before preparation, after preparation, after remote acceptance and after committed completion. Only provably unsent work retries automatically. Indeterminate attempts retain reservations and require warned retry. Old fencing tokens cannot commit drafts, ledgers or index manifests.
- **R4:** Test fact removal with retained raw-Q&A visibility, source deletion with linked-fact revocation, explicit standalone conversion, queued extraction suppression, interrupted cleanup and post-deletion citation inspection.
- **C1:** No-key/no-consent Q&A makes zero cloud calls. Selected-source boundaries reject foreign documents; revocation between calls stops assessment. Two-call ceiling holds; insufficient support abstains, and raw Q&A never confirms applicant facts.
