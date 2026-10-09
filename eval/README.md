# Frozen synthetic retrieval evaluation

`cases.json` v2 labels were frozen **before** running the models; no post-run
parameter or label tuning. Ten facts and twelve English queries cover tech,
finance, duplicate Python API evidence, conflicting team-size quotations, a
Python-snake lexical distractor and two deliberately unanswerable questions.
Contradictory records are both relevant quotations, not resolved truth.

After explicit model setup, run from repository root:

```sh
PYTHONPATH=backend uv run --project backend python eval/run.py --models /path/to/models --output eval/latest-smoke.json
PYTHONPATH=backend uv run --project backend python -m unittest discover -s eval -p test_metrics.py
COPILOT_REAL_MODEL_DIR=/path/to/models PYTHONPATH=backend uv run --project backend python -m pytest backend/tests/test_retrieval.py -m real_models
```

The runner uses actual pinned local MiniLM embeddings, local CrossEncoder weights,
real Chroma PersistentClient (test-only), saved BM25, RRF and reranking. Reported
Recall@8, binary nDCG@8, truncated MRR@8 and top1 accuracy average only the ten
answerable queries. Duplicate fact keys are distinct relevant records. The report
records returned fact keys and runtime IDs for each baseline and hybrid ranking.
Unanswerable queries are unscored, and their returned results are explicitly **not
semantic abstention** or support for a requested claim.

Warm p50/p95 (nearest-rank p95) cover only twelve queries over ten short chunks,
with model loading measured separately in cold build time. This is not a
1000-chunk performance benchmark, production relevance gate, semantic-support
assessment or hiring-quality evaluation. Scores are not truth probabilities.
The dataset checksum binds this run to its frozen labels. Docker is not tested by
this evaluation; the separate opt-in HTTP adapter test exercises the deployed
client transport without claiming container verification.

## Recorded v2 run (not a release gate)

The saved run reports Recall@8 **1.0**, nDCG@8 **0.9693**, MRR@8 **0.95** and
answerable top1 accuracy **0.90** for all three branches. There is **no measured
hybrid advantage on this tiny set**. All branches rank the snake distractor first
for the negated software-not-wildlife query, demonstrating a real relevance
failure; labels and parameters were not changed to hide it. Both unanswerable
queries still return results. Further grounded answer generation therefore must
implement its own support assessment/abstention gates, not treat retrieval scores
as proof. The saved warm p50/p95 are about 1.30/1.99 seconds over ten short chunks,
not the promised larger-corpus performance requirement.

## Frozen application-input runner (partial S4 evidence)

The separate `application_run.py` accepts only the exact 60-case synthetic freeze
in `applications-frozen.jsonl`. It loads each confirmed-fact bank into its own
temporary owner, randomizes source/criterion presentation reproducibly, and runs
the full-question query compiler against real local embeddings, Chroma, BM25 and
reranking. Labels, source keys, pending proposals and scoring metadata do not enter
retrieval. Query variants are combined by per-branch RRF. Runtime UUID/tie behavior
is not made deterministic by the presentation seed.

```sh
PYTHONPATH=backend uv run --no-sync --project backend python eval/application_run.py \
  --dataset eval/applications-frozen.jsonl --models /path/to/models --offline \
  --split tune --output /tmp/copilot-application-tune.json
PYTHONPATH=backend uv run --no-sync --project backend python -m unittest discover -s eval -p 'test_*.py'
```

The `--offline` CLI now starts a fresh isolated Python worker in an empty temporary
working directory, with explicit repository import paths and an allowlisted
OS-only environment. It does not inherit caller `.env`, `PYTHONPATH`, Chroma,
proxy, credential or provider settings. Model-library offline switches are forced;
Chroma uses explicit local Rust persistent settings with anonymized telemetry off.
Dataset, model and output paths are resolved before the worker changes directory.
The parent never imports Chroma or model libraries, and worker failure propagates
without a success report. This is a configuration-isolation boundary, not an OS
network sandbox; regression tests deny sockets while exercising real local Chroma
with synthetic model doubles. No model-quality qualification follows from them.

On POSIX, parent-only `SIGTERM` and Python interruption unwind through exact-owned
child termination and reaping before removing the parent worker directory. Worker
data/index temporary roots are nested beneath that directory, so abrupt child
termination cannot leave them outside the parent's cleanup scope. An unresponsive
child is killed after a five-second termination grace period; no process-group
kill is used. The temporary parent `SIGTERM` handler is restored after cleanup.
`SIGKILL`, host crashes, and Windows termination are outside this POSIX guarantee.

The low-level `run(cases, root, models)` hook is for controlled isolated test
processes only, not a concurrent service entry point: callers must supply a clean
environment and a cwd without `.env` **before** importing runtime libraries. It
fails closed on cwd `.env` or ambient `CHROMA_*`; it never changes global cwd/env.
Use the CLI for the supported offline boundary.

The historical first real-model **tune-only** run (before this isolation repair)
measured 30 cases and 192 canonical citation
checks. Answerable MRR@8 was dense **0.9167**, BM25 **0.7778**, hybrid **0.8333**;
there is no demonstrated hybrid ranking advantage. Recall@8 was 1.0 in all three
branches, but most isolated banks fit entirely inside eight results, making that
weak evidence. The unchanged software-not-snakes regression still ranks wildlife
first in all branches. No thresholds or labels were tuned to conceal this result.

A fresh isolated-CLI tune run on 9 October 2026 again checked 30 cases and 192
citations: dense MRR@8 **0.9167**, BM25 **0.7500**, hybrid **0.8333**. BM25 changed
between runs; runtime UUID/tie ordering is not fixed, so this is not a claim of
deterministic rankings or quality improvement. No labels or parameters changed.
Wildlife still ranks first in the negation regression;
semantic abstention and release qualification remain unassessed/false. No held-out
case was run, and no paid request was made.

This is personal quotation retrieval only: employer excerpts are synthetic query
criteria, not an indexed fetched-employer corpus. Semantic abstention, factual-span
support and grounded answers are **not assessed**; release qualification is false.
The held-out split has not been run through this component. Keep it separate from
tuning and reserve qualification for the completed pipeline. The original input
manifest records freeze-time status, not a current release verdict. Related tune/
held-out templates, small banks, test-only PersistentClient transport and absent
human/editorial/1,000-chunk gates remain explicit limitations.
