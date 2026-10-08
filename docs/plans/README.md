# Application Copilot — current roadmap and implementation evidence

## Implemented foundation

The preserved Node MVP covers onboarding, confirmation-gated memory and application research/drafting. The additive Python evidence workbench covers deliberate TXT/Markdown/text-PDF import, immutable confirmed facts, Chroma dense + BM25/RRF/local reranking and inspectable canonical citations. The two experiences are **not yet integrated**.

- [Evidence milestone](evidence-milestone.md)
- [Foundation verification contract](evidence-milestone-tests.md)
- [Observed implementation evidence and remaining release gates](evidence-verification.md)

Docker scaffolding exists, but runtime deployment is not verified. Citation integrity is not semantic truth; the current Python endpoint returns quotations, not generated answers. The small evaluation exposes a negation failure and lacks semantic abstention. Do not call this foundation production-ready.

## Reviewed next implementation plan

1. **A — researched, grounded application drafting:** unify the Python domain/UI; retain original company/exact-vacancy sources; retrieve separate personal/employer evidence per question; assess exact answer text; human review and export.
2. **B — supervised browser preparation:** qualify a local headed companion and limited Greenhouse hosted-form adapter; authorize exact data before fill/autosave/upload.
3. **C — approved agent submission:** separate one-use approval of the exact completed application, one attempted final action and verified receipt or honest unknown state. No unattended bulk submission.

- [Detailed product and architecture plan](prd-researched-applications.md)
- [Quality, security and recovery test specification](test-spec-researched-applications.md)
- [Implementation slices, ownership, dependencies and stop conditions](implementation-slices-researched-applications.md)
- [Sequential review disposition and exact artifact hashes](researched-applications-review.md)

These are **planned features, not implemented capabilities or permission to perform live applications**. Each friend runs an independent local installation with their own data/key and explicit cloud opt-in. Application writing supports tech and finance; browser support is adapter-specific, not universal. Unsupported forms remain manual.

## Earlier design records

- [Original local RAG product vision](local-rag-mvp.md)
- [Original verification specification](local-rag-tests.md)

The newer reviewed plan supersedes their manual-only submission boundary for the future separately gated browser release. Earlier documents remain historical design records.

Private documents, credentials, local data, model caches and machine-specific orchestration artifacts remain excluded. Public plans and review notes establish design evidence, not host-security authority, deployment qualification or execution-authorization receipts.
