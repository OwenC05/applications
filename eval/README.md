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

The `--offline` CLI uses the shared verified SDK launch boundary: a fresh isolated
Python child with explicit repository import paths, an empty owned lifetime CWD
and an allowlisted OS-only environment. It does not inherit caller `PYTHONPATH`,
Chroma, proxy, credential or provider settings; model-library offline switches are
forced. Chroma uses explicit local Rust persistent settings with telemetry off.
Dataset, model and report paths resolve in the caller before launch. The parent
does not import Chroma/model libraries. This is configuration isolation, not an OS
network sandbox or model-quality certification.

Evaluation data/indexes live in a **separate parent-created owned workspace**,
outside the lifetime CWD and disjoint from approved model/dataset/output paths.
After exact child reaping and group-quiescence proof, only that verified workspace
is recursively cleaned using descriptor-relative, symlink- and mount-aware
operations. The empty lifetime CWD is removed with `rmdir` only. Unexpected identity,
contents or uncertain termination retain the affected roots with a safe warning.
Explicit report output is never a cleanup target.

On POSIX, SIGINT/SIGTERM request termination of the exact new-session child group;
application evaluation forwards SIGTERM for either interruption, preserving parent
exit 130/143. It retains a five-second termination grace, then KILL and a
single bounded one-second reaping budget; parent-directed exits map to 130/143.
Prior parent handlers are restored. Regression tests cover graceful and forced
termination, separate workspace placement, no late report, reaping before both
roots disappear and preservation of foreign sibling canaries. Parent SIGKILL,
host crashes and native Windows are outside this guarantee. A cleanup warning
following completed work does not imply rollback or an automatic retry.

The low-level `run(cases, root, models)` hook now requires an already verified SDK
child boundary as well as its existing clean/offline configuration checks. An
arbitrary process with a missing `.env` is not eligible; no temporary global
CWD/environment mutation or unsafe embedding flag is provided. Use the CLI for
the supported offline boundary. Direct pytest/tool collection remains responsible
for its own controlled import CWD.

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
