# Next iteration: local production-minded RAG

This directory preserves the reviewed plan before any new implementation. The current code remains the Node.js MVP; Python, document ingestion, hybrid retrieval, Chroma and Docker are not yet implemented.

- [Product and architecture plan](local-rag-mvp.md)
- [Verification specification](local-rag-tests.md)

## Agreed stack and scope

Python/FastAPI, SQLite canonical records, Chroma dense vectors, BM25S lexical retrieval, reciprocal-rank fusion and local cross-encoder reranking. Docker Compose runs a private local installation for each friend, with separate data, their own backend API key and explicit cloud-generation consent. Reuse the existing browser UI.

First milestone: explicit TXT/Markdown/text-PDF imports, confirmed facts, hybrid retrieval and inspectable citations. Second: actual company/exact-role research, tailored application drafts, exact-text support assessment, human review and export. Submission remains manual.

Citation integrity verifies an immutable source/fact version, hash, location and exact excerpt; it does not certify semantic truth. Personal facts remain confirmation-gated, edited answers require fresh support assessment, and deleted or stale evidence cannot return through indexes or late jobs.

## Review and evidence boundary

Planner, Architect and Critic completed sequential reviews; the second review cycle approved this plan. Implementation must still prove dependency compatibility, citation support, deletion/recovery, local CPU performance and friend setup. Historical Node test results do not verify the proposed stack.

Machine-specific orchestration state, private documents, actual keys, local data and generated outputs are intentionally excluded from this repository. These documents are design records, not deployment or execution-authorization receipts.
