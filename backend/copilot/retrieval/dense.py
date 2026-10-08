"""Chroma adapter: embeddings are explicit, never server-default generated."""
import hashlib

from copilot.contracts import EvidenceError


def ids_checksum(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


class DenseIndex:
    def __init__(self, client):
        self.client = client

    def create(self, name, chunks, vectors, fingerprint, profile_id, generation_id):
        collection = self.client.create_collection(name, embedding_function=None,
            metadata={"fingerprint": fingerprint, "profile_id": profile_id,
                      "generation_id": generation_id, "ids_checksum": ids_checksum([c.id for c in chunks])},
            configuration={"hnsw": {"space": "cosine"}})
        for start in range(0, len(chunks), 100):
            group = chunks[start:start + 100]
            collection.add(ids=[c.id for c in group], embeddings=vectors[start:start + 100],
                           metadatas=[{"record_id": c.record_id} for c in group])

    def get(self, name):
        try:
            return self.client.get_collection(name, embedding_function=None)
        except Exception as exc:
            raise EvidenceError("INDEX_NOT_READY", "Dense index unavailable; rebuild required", 503) from exc

    def verify(self, name, ids, fingerprint):
        collection = self.get(name)
        actual = collection.get(include=[])["ids"]
        meta = collection.metadata or {}
        if (set(actual) != set(ids) or len(actual) != len(ids)
                or meta.get("fingerprint") != fingerprint
                or meta.get("ids_checksum") != ids_checksum(ids)):
            raise EvidenceError("INDEX_NOT_READY", "Dense index integrity mismatch; rebuild required", 503)

    def query(self, name, vector, limit, source_ids=None):
        where = None
        if source_ids is not None:
            where = {"record_id": {"$in": source_ids}}
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
