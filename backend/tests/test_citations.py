from dataclasses import replace

import pytest
from test_store import chunks

from copilot.contracts import EvidenceError, Span
from copilot.store import Store


def test_canonical_citations_are_not_semantic_proof(tmp_path):
    store = Store(tmp_path)
    p = store.create_profile('A', ['tech'])
    q = store.create_profile('B', ['finance'])
    s = store.add_source(p.id, 'notes.md', 'text/markdown', 'first\r\nUnicode café\n'.encode(),
                         ['first\r\nUnicode café\n'], 'test')
    snap = store.snapshot(p.id, 'documents', chunks)
    c = store.citation_for_chunk(p.id, snap.chunks[0])
    resolved = store.resolve_citation(p.id, c)
    assert resolved['canonical_text'] == 'first\nUnicode café\n'
    assert resolved['line_start'] == 1 and resolved['line_end'] == 2
    assert resolved['semantic_support'] == 'not_assessed'
    for bad in [replace(c, text_sha256='0' * 64), replace(c, excerpt='wrong'),
                replace(c, start=-1), replace(c, end=999), replace(c, profile_id=q.id),
                replace(c, unit_id=s.id), replace(c, corpus='bad')]:
        with pytest.raises(EvidenceError):
            store.resolve_citation(p.id, bad)
    with pytest.raises(EvidenceError):
        store.resolve_citation(q.id, c)
    u = store.list_units(p.id, s.id)[0]
    store.confirm_fact(p.id, 'Corrected claim', Span(u.id, 0, 5))
    fs = store.snapshot(p.id, 'facts', chunks)
    fc = store.citation_for_chunk(p.id, fs.chunks[0])
    assert store.resolve_citation(p.id, fc)['origin_label'] == 'proposal origin, not semantic proof'
    store.revoke_fact(p.id, fc.record_id)
    with pytest.raises(EvidenceError):
        store.resolve_citation(p.id, fc)
    assert store.resolve_citation(p.id, c)['integrity'] == 'verified'
    store.delete_source(p.id, s.id)
    with pytest.raises(EvidenceError):
        store.resolve_citation(p.id, c)
