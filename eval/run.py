"""Frozen synthetic retrieval evaluation; returned quotes are not semantic truth."""
import argparse
import hashlib
import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import chromadb
from copilot.models import LocalModels
from copilot.retrieval import sparse
from copilot.retrieval.dense import DenseIndex
from copilot.retrieval.service import EvidenceService
from copilot.store import Store


def metrics(ranked, relevant, cutoff=8):
    """Binary fact-key relevance; unanswerables deliberately have no quality score."""
    relevant = set(relevant)
    if not relevant:
        return None
    ranked = list(dict.fromkeys(ranked))[:cutoff]
    positions = [i for i, key in enumerate(ranked, 1) if key in relevant]
    dcg = sum(1 / math.log2(i + 1) for i in positions)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(relevant), cutoff) + 1))
    return {"recall_at_8": len(positions) / len(relevant),
            "ndcg_at_8": dcg / ideal, "mrr_at_8": 1 / min(positions) if positions else 0,
            "top1_accuracy": float(bool(ranked) and ranked[0] in relevant)}


def summarize(results):
    report = {}
    for branch in ("dense", "bm25", "hybrid_reranked"):
        scored = [row["metrics"][branch] for row in results if row["metrics"][branch] is not None]
        report[branch] = {"answerable_queries": len(scored),
                          **{key: statistics.mean(row[key] for row in scored)
                             for key in ("recall_at_8", "ndcg_at_8", "mrr_at_8", "top1_accuracy")}}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset_bytes = Path(__file__).with_name("cases.json").read_bytes()
    cases = json.loads(dataset_bytes)
    assert cases["schema"] == 2
    with tempfile.TemporaryDirectory(prefix="copilot-synthetic-eval-") as temp:
        root = Path(temp)
        store = Store(root / "data")
        dense = DenseIndex(chromadb.PersistentClient(path=str(root / "chroma")))
        models = LocalModels(args.models)
        service = EvidenceService(store, root / "indexes", dense, models)
        profile = store.create_profile("Synthetic evaluation", ["tech", "finance"])
        keys = {store.confirm_fact(profile.id, case["text"]).id: case["key"]
                for case in cases["facts"]}
        started = time.perf_counter()
        manifest = service.build(profile.id, "facts")
        build_seconds = time.perf_counter() - started
        ids = service._ids(manifest)
        chunks = {c.id: c for c in store.eligible_chunks(profile.id, "facts", ids, None)}
        engine, meta = sparse.load(service.index_root / manifest.sparse_relpath, ids, models.fingerprint)
        results = []
        for case in cases["queries"]:
            query = case["query"]
            started = time.perf_counter()
            hits = service.search(profile.id, "facts", query)
            elapsed = time.perf_counter() - started
            rankings = {
                "dense": dense.query(manifest.dense_collection, models.embed([query])[0], 8),
                "bm25": sparse.query(engine, meta, query, 8),
                "hybrid_reranked": [hit.chunk.id for hit in hits],
            }
            returned = {branch: [{"fact_key": keys[chunks[key].record_id],
                                  "record_id": chunks[key].record_id, "chunk_id": key} for key in ranking]
                        for branch, ranking in rankings.items()}
            scores = {branch: metrics([row["fact_key"] for row in ranking], case["relevant"])
                      for branch, ranking in returned.items()}
            results.append({"key": case["key"], "query": query, "kind": case["kind"],
                            "relevant_fact_keys": case["relevant"], "metrics": scores,
                            "returned": returned,
                            "unanswerable_warning": ("Returned results are NOT semantic abstention or evidence that the requested claim is true"
                                                     if not case["relevant"] else None),
                            "retrieval_seconds": elapsed})
        latencies = sorted(row["retrieval_seconds"] for row in results)
        report = {"dataset": "frozen-v2: 10 synthetic facts, 12 queries, 2 unanswerable",
                  "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
                  "not_production_quality_evidence": True,
                  "contradictions_are_relevant_quotes_not_resolved_truth": True,
                  "unanswerables_excluded_from_quality_averages": True,
                  "metric_definition": "Binary unique fact-key relevance; @8; MRR truncated at8; top1 for answerables only",
                  "model_fingerprint": models.fingerprint,
                  "build_seconds": build_seconds,
                  "warm_latency_tiny_dataset_only": {"queries": len(latencies),
                      "p50_seconds": statistics.median(latencies),
                      "p95_seconds_nearest_rank": latencies[math.ceil(0.95 * len(latencies)) - 1],
                      "not_1000_chunk_benchmark": True},
                  "summary": summarize(results), "results": results}
        output = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(output + "\n")
        print(output)


if __name__ == "__main__":
    main()
