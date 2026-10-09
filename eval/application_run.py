"""Frozen synthetic personal-retrieval measurements, not grounded-answer quality.

Gold/split/category/answerability are scoring metadata, never retrieval inputs.
Employer excerpts provide untrusted query criteria, not fetched employer evidence.
No semantic assessor, paid request, threshold tuning or answer generation occurs.
"""
import argparse
import hashlib
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from run import metrics

FROZEN_SHA256 = '9a1e8944d876625213a05d4bb5557c35c8f02a012773e227d45ea446bfee23a6'
BRANCHES = ('dense', 'bm25', 'hybrid_reranked')
METRICS = ('recall_at_8', 'ndcg_at_8', 'mrr_at_8', 'top1_accuracy')


def load_cases(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_SHA256:
        raise ValueError('Only the exact frozen synthetic dataset is accepted')
    cases = [json.loads(line) for line in raw.splitlines()]
    if len(cases) != 60:
        raise ValueError('Frozen dataset count mismatch')
    return cases


def retrieval_input(case, seed):
    """Whitelist public question/criteria and owned confirmed synthetic wording."""
    def ordering(record):
        return hashlib.sha256(f'{seed}:{case["id"]}:{record["id"]}'.encode()).hexdigest()
    facts = case['confirmed_fact_bank']
    employers = case['employer_excerpts']
    if any(source['owner_id'] != case['owner_id'] or source['state'] != 'confirmed'
           or source['provenance'] != 'synthetic_confirmed_fact' for source in facts):
        raise ValueError('Ineligible synthetic fact scope')
    if any(source['owner_id'] != case['owner_id']
           or source['application_id'] != case['application_id']
           or source['fictional'] is not True
           or source['provenance'] != 'synthetic_canonical_excerpt' for source in employers):
        raise ValueError('Ineligible synthetic employer scope')
    return {'question': case['question'],
            'facts': [{'id': source['id'], 'text': source['text']}
                      for source in sorted(facts, key=ordering)],
            'criteria': [source['text'] for source in sorted(employers, key=ordering)]}


def rrf(rankings, cutoff=8):
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:cutoff]


def evaluation_summary(rows):
    result = {'semantic_abstention': 'not_assessed', 'release_qualified': False}
    for split in ('tune', 'heldout'):
        bucket = [row for row in rows if row['split'] == split]
        branches = {}
        for branch in BRANCHES:
            scored = [row['metrics'][branch] for row in bucket
                      if row['metrics'][branch] is not None and row['answerable']]
            branches[branch] = {'answerable_cases': len(scored),
                               **{name: statistics.mean(score[name] for score in scored)
                                  if scored else None for name in METRICS}}
        result[split] = {'cases': len(bucket),
                         'unanswerable': sum(not row['answerable'] for row in bucket),
                         'branches': branches}
    return result


def run_case(case, seed, store, service):
    from copilot.domain.contracts import Question
    from copilot.retrieval import sparse
    from copilot.retrieval.queries import compile_queries

    selected = retrieval_input(case, seed)
    # Fresh isolated owner per case; never combine source banks across fixtures.
    profile = store.create_profile('Synthetic application evaluation', [case['sector']])
    fixture_by_runtime = {}
    for source in selected['facts']:
        fact = store.confirm_fact(profile.id, source['text'])
        fixture_by_runtime[fact.id] = source['id']
    compilation = compile_queries(Question(id=case['id'], text=selected['question'],
                                          type='writing', constraint_origin='user'),
                                  service.models.tokenize_offsets, selected['criteria'])
    started = time.perf_counter()
    manifest = service.build(profile.id, 'facts')
    build_seconds = time.perf_counter() - started
    ids = service._ids(manifest)
    chunks = {chunk.id: chunk for chunk in store.eligible_chunks(
        profile.id, 'facts', ids, generation_id=manifest.generation_id)}
    engine, metadata = sparse.load(service._manifest_path(manifest), ids,
                                  service.models.fingerprint)
    rankings = {branch: [] for branch in BRANCHES}
    checked = 0
    started = time.perf_counter()
    for query in compilation.variants:
        hits = service.search(profile.id, 'facts', query)
        for hit in hits:
            if not store.resolve_citation(profile.id, hit.citation):
                raise ValueError('Canonical citation integrity failed')
            checked += 1
        candidates = {
            'dense': service.dense.query(manifest.dense_collection,
                                        service.models.embed([query])[0], 8),
            'bm25': sparse.query(engine, metadata, query, 8),
            'hybrid_reranked': [hit.chunk.id for hit in hits],
        }
        for branch, keys in candidates.items():
            if not set(keys) <= set(chunks):
                raise ValueError('Retriever returned out-of-scope chunks')
            rankings[branch].append([fixture_by_runtime[chunks[key].record_id] for key in keys])
    store.validate_snapshot(profile.id, manifest.revision, 'facts')
    elapsed = time.perf_counter() - started
    returned = {branch: rrf(variants) for branch, variants in rankings.items()}
    row = {'id': case['id'], 'key': case['key'], 'split': case['split'],
           'sector': case['sector'], 'answerable': case['answerable'],
           'adversarial': case['adversarial'], 'category': case['category'],
           'query_variants': list(compilation.variants), 'omitted': list(compilation.omitted),
           'confirmed_bank_size': len(selected['facts']), 'returned_fixture_ids': returned,
           'metrics': {branch: metrics(keys, case['gold']['retrieval_relevant_ids'])
                       for branch, keys in returned.items()},
           'citation_integrity_checked': checked, 'semantic_decision': 'not_assessed',
           'build_seconds': build_seconds, 'retrieval_seconds': elapsed}
    ticket = store.delete_profile(profile.id)
    service.cleanup(ticket)
    return row


def run(cases, root, models, seed=17):
    """Low-level test hook: caller must supply a clean environment and empty cwd.

    Use main() for the supported offline boundary; this function does not mutate
    a reusable process's environment or working directory to create isolation.
    """
    if Path('.env').exists() or any(key.upper().startswith('CHROMA_') for key in os.environ):
        raise RuntimeError('run requires an isolated environment; use the offline CLI')
    import chromadb
    from chromadb.config import Settings
    from copilot.retrieval.dense import DenseIndex
    from copilot.retrieval.service import EvidenceService
    from copilot.store import Store

    store = Store(root / 'data')
    dense = DenseIndex(chromadb.PersistentClient(
        path=str(root / 'chroma'),
        settings=Settings(_env_file=None, anonymized_telemetry=False,
                          chroma_api_impl='chromadb.api.rust.RustBindingsAPI',
                          is_persistent=True)))
    service = EvidenceService(store, root / 'indexes', dense, models)
    rows = [run_case(case, seed, store, service) for case in cases]
    latencies = sorted(row['retrieval_seconds'] for row in rows)
    return {'schema_version': 1, 'dataset_sha256': FROZEN_SHA256,
            'model_fingerprint': models.fingerprint, 'presentation_seed': seed,
            'scope': 'personal quotation retrieval only; no employer indexing or semantic assessment',
            'query_policy': 'full question plus separate synthetic employer criteria; RRF across variants per branch',
            'not_production_quality_evidence': True,
            'limitations': [
                '19/30 heldout questions match tune templates; no generalisation claim',
                'Small isolated banks often fit entirely inside cutoff8; recall can be trivial',
                'Seed randomises fact/criterion presentation, not runtime UUID/tie behaviour',
                'Employer excerpts are synthetic, not original fetched evidence',
                'No answer generation, factual-span/human audit or editorial comparison',
                'Semantic abstention and original negation support remain unassessed',
                'PersistentClient test transport, not deployed HTTP/Docker qualification',
            ],
            'summary': evaluation_summary(rows),
            'tiny_case_latency_not_1000_chunk_benchmark': {
                'p50_seconds': statistics.median(latencies),
                'p95_seconds_nearest_rank': latencies[math.ceil(0.95 * len(latencies)) - 1]},
            'results': rows}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--offline', action='store_true', required=True)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--split', choices=('tune', 'heldout', 'all'), default='tune')
    parser.add_argument('--output', type=Path, required=True)
    return parser.parse_args(argv)


def _worker_main(argv):
    args = _parse_args(argv)
    cases = load_cases(args.dataset)
    cases = [case for case in cases if args.split == 'all' or case['split'] == args.split]
    from copilot.models import LocalModels
    with tempfile.TemporaryDirectory(prefix='copilot-frozen-app-eval-', dir=Path.cwd()) as temp:
        report = run(cases, Path(temp), LocalModels(args.models), args.seed)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output': str(args.output), 'dataset_sha256': FROZEN_SHA256,
                      'summary': report['summary']}, allow_nan=False))


def main():
    # Parse before spawning or importing model/storage libraries. Resolve caller
    # paths before changing cwd in the child; never read the caller's dotenv.
    args = _parse_args()
    worker_args = ['--dataset', str(args.dataset.resolve()), '--models', str(args.models.resolve()),
                   '--offline', '--seed', str(args.seed), '--split', args.split,
                   '--output', str(args.output.resolve())]
    repo = Path(__file__).resolve().parents[1]
    bootstrap = (f'import sys; sys.path[:0] = {[str(repo / "eval"), str(repo / "backend")]!r}; '
                 'from application_run import _worker_main; _worker_main(sys.argv[1:])')
    # No inherited dotenv, transport, auth, proxy, provider or Python-path config.
    # Retain only OS process necessities; the model loader remains hash-checked
    # and local_files_only, with library offline switches forced as well.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE'}}
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               ANONYMIZED_TELEMETRY='False')
    child = None
    termination_requested = None

    def terminate_parent(signum, frame):
        nonlocal termination_requested
        # Repeated TERM must not interrupt exact-child reaping/root cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        termination_requested = signum
        if child is not None:
            raise SystemExit(128 + signum)

    previous_handler = None
    if os.name == 'posix':
        previous_handler = signal.signal(signal.SIGTERM, terminate_parent)
    try:
        with tempfile.TemporaryDirectory(prefix='copilot-offline-worker-') as temp:
            child = None
            try:
                # During Popen the handler records TERM rather than raising
                # before ownership is assigned. Do not block SIGTERM: a child
                # would inherit that mask and ignore terminate() until unblocked.
                if termination_requested is not None:
                    raise SystemExit(128 + termination_requested)
                child = subprocess.Popen([sys.executable, '-I', '-c', bootstrap, *worker_args],
                                         cwd=temp, env=env)
                if termination_requested is not None:
                    raise SystemExit(128 + termination_requested)
                returncode = child.wait()
                if returncode:
                    raise subprocess.CalledProcessError(returncode, child.args)
            finally:
                # Only this owned process is signalled. Reap before removing its
                # cwd, which also contains all worker data/index temporary roots.
                if child is not None:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                    else:
                        child.wait()
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == '__main__':
    main()
