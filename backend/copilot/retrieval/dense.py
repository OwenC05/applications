"""Chroma adapter: embeddings are explicit, never server-default generated."""
import hashlib

from copilot.contracts import EvidenceError


def scope_metadata(scope, generation_id):
    result = {"profile_id": scope.profile_id, "corpus": scope.corpus,
              "generation_id": generation_id}
    if scope.corpus == "employer":
        result.update(application_id=scope.application_id, research_run_id=scope.research_run_id)
    return result


def ids_checksum(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


class DenseIndex:
    def __init__(self, client):
        self.client = client

    def create(self, name, chunks, vectors, fingerprint, profile_id, generation_id):
        metadata = {"fingerprint": fingerprint, "profile_id": profile_id,
                    "generation_id": generation_id, "corpus": chunks[0].corpus,
                    "ids_checksum": ids_checksum([c.id for c in chunks])}
        if chunks[0].corpus == "employer":
            metadata.update(application_id=chunks[0].application_id,
                            research_run_id=chunks[0].research_run_id)
        collection = self.client.create_collection(name, embedding_function=None,
            metadata=metadata,
            configuration={"hnsw": {"space": "cosine"}})
        for start in range(0, len(chunks), 100):
            group = chunks[start:start + 100]
            collection.add(ids=[c.id for c in group], embeddings=vectors[start:start + 100],
                           metadatas=[{"record_id": c.record_id, **metadata} for c in group])

    def get(self, name):
        try:
            return self.client.get_collection(name, embedding_function=None)
        except Exception as exc:
            raise EvidenceError("INDEX_NOT_READY", "Dense index unavailable; rebuild required", 503) from exc

    def verify(self, name, ids, fingerprint, *, scope=None, generation_id=None, records=None):
        collection = self.get(name)
        result = collection.get(include=["metadatas"] if scope is not None else [])
        actual = result["ids"]
        meta = collection.metadata or {}
        if (set(actual) != set(ids) or len(actual) != len(ids)
                or meta.get("fingerprint") != fingerprint
                or meta.get("ids_checksum") != ids_checksum(ids)):
            raise EvidenceError("INDEX_NOT_READY", "Dense index integrity mismatch; rebuild required", 503)
        if scope is not None:
            expected = scope_metadata(scope, generation_id)
            rows = result.get("metadatas") or []
            if (len(rows) != len(actual) or any(meta.get(k) != v for k, v in expected.items())
                    or any(not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items())
                           or row.get("fingerprint") != fingerprint
                           or row.get("ids_checksum") != ids_checksum(ids)
                           for row in rows)
                    or (records is not None and any(row.get("record_id") != records[key]
                        for key, row in zip(actual, rows, strict=True)))):
                raise EvidenceError("INDEX_NOT_READY", "Dense index scope mismatch", 503)

    def query(self, name, vector, limit, source_ids=None, *, scope=None, generation_id=None):
        where = None
        if source_ids is not None:
            where = {"record_id": {"$in": source_ids}}
        if scope is not None:
            filters = [{key: value} for key, value in scope_metadata(scope, generation_id).items()]
            if where is not None:
                filters.append(where)
            where = {"$and": filters}
        collection = self.get(name)
        candidate_count = len(collection.get(where=where, include=[])["ids"])
        if not candidate_count:
            return []
        # At the bounded 1000-chunk milestone retrieve all distances so ties at
        # the top-k boundary are reproducibly resolved by canonical chunk ID.
        result = collection.query(query_embeddings=[vector], n_results=candidate_count,
                                  where=where, include=["distances"])
        pairs = zip(result["ids"][0], result["distances"][0], strict=True)
        return [item[0] for item in sorted(pairs, key=lambda pair: (pair[1], pair[0]))[:limit]]

    def delete(self, name):
        if name in self.names():
            self.client.delete_collection(name)
        if name in self.names():
            raise EvidenceError("CLEANUP_PENDING", "Dense cleanup not confirmed", 503)

    def names(self):
        return [item.name if hasattr(item, "name") else str(item)
                for item in self.client.list_collections()]

    def owned_names(self, profile_id):
        return [name for name in self.names()
                if (self.get(name).metadata or {}).get("profile_id") == profile_id]
