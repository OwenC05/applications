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
