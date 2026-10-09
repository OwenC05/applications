"""Synthetic HTTP boundary tests; fake service does not prove retrieval quality."""
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient
from test_store import chunks

from copilot.api import create_app
from copilot.config import Settings
from copilot.contracts import EvidenceError


class FakeService:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def search(self, profile_id, corpus, query, source_ids, limit):
        self.calls.append((profile_id, corpus, query, source_ids, limit))
        return []

    def cleanup(self, ticket):
        raise EvidenceError("INDEX_NOT_READY", "Synthetic index unavailable", 503)

    def recover(self):
        return None


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path / "data", tmp_path / "models"),
                     service_factory=lambda store, _settings: FakeService(store))
    with TestClient(app, base_url="http://127.0.0.1:3001") as client:
        client.headers["x-evidence-token"] = client.get("/api/evidence/status").json()["token"]
        yield client


def profile(client, name="Synthetic", sectors=None):
    response = client.post("/api/evidence/profiles", json={"name": name, "sectors": sectors or ["tech"]})
    assert response.status_code == 200
    return response.json()["id"]


def source(client, owner, content=b"Synthetic evidence\r\nsecond line"):
    response = client.post(f"/api/evidence/profiles/{owner}/sources",
                           files={"file": ("notes.md", content, "text/markdown")})
    assert response.status_code == 200
    return response.json()["id"]


def test_health_and_status_do_not_initialize_service_or_download_models(tmp_path):
    def unavailable(*_args):
        pytest.fail("Health/status must not require an index or model service")
    app = create_app(Settings(tmp_path / "data", tmp_path / "models"), unavailable)
    with TestClient(app, base_url="http://127.0.0.1:3001") as client:
        assert client.get("/health").json() == {"status": "alive", "mode": "quote_only"}
        status = client.get("/api/evidence/status").json()
        assert status["models_ready"] is False
        assert status["cloud_enabled"] is False
        assert status["pending_cleanup"] == 0
        assert not (tmp_path / "models").exists()


def test_profiles_tech_finance_and_pending_deletion(client):
    first = profile(client, "Engineer", ["tech"])
    second = profile(client, "Analyst", ["finance"])
    assert {p["id"] for p in client.get("/api/evidence/profiles").json()} == {first, second}
    response = client.delete(f"/api/evidence/profiles/{first}")
    assert response.status_code == 202 and response.json()["state"] == "pending"
    assert [p["id"] for p in client.get("/api/evidence/profiles").json()] == [second]


def test_upload_preview_preserves_original_and_never_serves_inline_html(client):
    owner = profile(client)
    content = b"<script>alert('synthetic')</script>\r\nEvidence"
    document = source(client, owner, content)
    units = client.get(f"/api/evidence/profiles/{owner}/sources/{document}/units").json()
    assert units[0]["text"] == content.decode().replace("\r\n", "\n")
    response = client.get(f"/api/evidence/profiles/{owner}/sources/{document}/original")
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "object-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("confirmation", [None, False, "true", 1])
def test_fact_requires_literal_confirmation(client, confirmation):
    owner = profile(client)
    body = {"text": "Synthetic confirmed statement"}
    if confirmation is not None:
        body["confirmed"] = confirmation
    response = client.post(f"/api/evidence/profiles/{owner}/facts", json=body)
    assert response.status_code in {400, 422}
    assert client.get(f"/api/evidence/profiles/{owner}/facts").json() == []


def test_confirmed_fact_is_exact_and_revoked_before_failed_cleanup(client):
    owner = profile(client)
    text = "Built a synthetic finance risk tool — café."
    response = client.post(f"/api/evidence/profiles/{owner}/facts", json={"text": text, "confirmed": True})
    assert response.status_code == 200 and response.json()["text"] == text
    response = client.delete(f"/api/evidence/profiles/{owner}/facts/{response.json()['id']}")
    assert response.status_code == 202
    assert client.get(f"/api/evidence/profiles/{owner}/facts").json() == []
    assert client.get("/api/evidence/status").json()["pending_cleanup"] == 1


def test_foreign_source_and_citation_are_not_accessible(client):
    owner, other = profile(client), profile(client, "Other")
    document = source(client, owner)
    store = client.app.state.store
    snapshot = store.snapshot(owner, "documents", chunks)
    citation = asdict(store.citation_for_chunk(owner, snapshot.chunks[0]))
    citation.pop("profile_id")
    prefix = f"/api/evidence/profiles/{other}/sources/{document}"
    for endpoint in ("units", "original"):
        assert client.get(f"{prefix}/{endpoint}").status_code >= 400
    assert client.post(f"/api/evidence/profiles/{other}/citations", json=citation).status_code >= 400
    response = client.post(f"/api/evidence/profiles/{owner}/citations", json=citation)
    assert response.json()["integrity"] == "verified"
    assert response.json()["semantic_support"] == "not_assessed"


def test_source_delete_revokes_access_with_unavailable_service(tmp_path):
    def unavailable(*_args):
        raise RuntimeError("synthetic unreachable Chroma")
    app = create_app(Settings(tmp_path / "data", tmp_path / "models"), unavailable)
    with TestClient(app, base_url="http://127.0.0.1:3001") as client:
        client.headers["x-evidence-token"] = client.get("/api/evidence/status").json()["token"]
        owner = profile(client)
        document = source(client, owner)
        response = client.delete(f"/api/evidence/profiles/{owner}/sources/{document}")
        assert response.status_code == 202
        assert client.get(f"/api/evidence/profiles/{owner}/sources").json() == []
        assert client.get(f"/api/evidence/profiles/{owner}/sources/{document}/original").status_code >= 400
        response = client.post(f"/api/evidence/profiles/{owner}/search", json={"corpus": "facts", "query": "risk"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "INDEX_NOT_READY"


@pytest.mark.parametrize("headers,code", [
    ({"host": "attacker.example:3001"}, "INVALID_HOST"),
    ({"origin": "https://attacker.example"}, "INVALID_ORIGIN"),
    ({"x-evidence-token": "wrong"}, "INVALID_TOKEN"),
])
def test_mutation_rejects_invalid_local_boundary(client, headers, code):
    response = client.post("/api/evidence/profiles", headers=headers,
                           json={"name": "Synthetic", "sectors": ["tech"]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == code
    assert client.get("/api/evidence/profiles").json() == []


def test_request_size_quota_rejects_before_mutation(client):
    response = client.post("/api/evidence/profiles", content=b"x" * 131073)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "LIMIT_EXCEEDED"
    assert client.get("/api/evidence/profiles").json() == []


def test_validation_errors_do_not_echo_private_inputs(client):
    marker = "synthetic-private-marker"
    response = client.post("/api/evidence/profiles", json={"name": marker, "sectors": [marker]})
    assert response.status_code == 422
    assert marker not in response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_search_contract_is_quote_only_and_forwards_selected_sources(client):
    owner = profile(client)
    document = source(client, owner)
    response = client.post(f"/api/evidence/profiles/{owner}/search",
                           json={"corpus": "documents", "query": "risk", "source_ids": [document], "limit": 2})
    assert response.json() == {"mode": "quotation_not_answer", "hits": []}
    assert client.app.state.service.calls == [(owner, "documents", "risk", [document], 2)]


def test_citation_rejects_altered_excerpt_and_revoked_source(client):
    owner = profile(client)
    document = source(client, owner)
    store = client.app.state.store
    snapshot = store.snapshot(owner, "documents", chunks)
    citation = asdict(store.citation_for_chunk(owner, snapshot.chunks[0]))
    citation.pop("profile_id")
    endpoint = f"/api/evidence/profiles/{owner}/citations"
    tampered = dict(citation, excerpt="Invented synthetic claim")
    assert client.post(endpoint, json=tampered).status_code >= 400
    assert client.post(endpoint, json=citation).status_code == 200
    assert client.delete(f"/api/evidence/profiles/{owner}/sources/{document}").status_code == 202
    assert client.post(endpoint, json=citation).status_code >= 400


def test_source_deletion_revokes_linked_fact_but_does_not_confirm_raw_text(client):
    owner = profile(client)
    document = source(client, owner)
    prefix = f"/api/evidence/profiles/{owner}"
    assert client.get(f"{prefix}/facts").json() == []
    unit = client.get(f"{prefix}/sources/{document}/units").json()[0]
    response = client.post(f"{prefix}/facts", json={
        "text": "Corrected synthetic statement", "confirmed": True,
        "origin": {"unit_id": unit["id"], "start": 0, "end": 9},
    })
    assert response.status_code == 200
    assert client.delete(f"{prefix}/sources/{document}").status_code == 202
    assert client.get(f"{prefix}/facts").json() == []


def test_api_search_never_runs_startup_external_recovery(tmp_path):
    class RecoveringService(FakeService):
        def recover(self):
            pytest.fail('Recovery belongs to the durable worker, not HTTP requests')
    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models'),
                     lambda store, _settings: RecoveringService(store))
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        client.headers['x-evidence-token'] = client.get('/api/evidence/status').json()['token']
        owner = profile(client)
        path = f'/api/evidence/profiles/{owner}/search'
        response = client.post(path, json={'corpus': 'facts', 'query': 'Synthetic'})
        assert response.status_code == 200
        assert response.json()['hits'] == []
