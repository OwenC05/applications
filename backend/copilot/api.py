"""Quote-only evidence API, separate from the legacy application workflow."""
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .config import Settings, data_lock
from .contracts import Citation, EvidenceError, Span
from .documents import ingest
from .store import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileInput(Input):
    name: str = Field(min_length=1, max_length=200)
    sectors: list[Literal["tech", "finance"]] = Field(min_length=1, max_length=2)


class FactInput(Input):
    text: str = Field(min_length=1, max_length=20000)
    confirmed: StrictBool
    origin: dict | None = None
    supersedes: str | None = None


class SearchInput(Input):
    corpus: Literal["facts", "documents"]
    query: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] | None = Field(default=None, max_length=100)
    limit: int = Field(default=8, ge=1, le=8)


class CitationInput(Input):
    corpus: Literal["facts", "documents"]
    record_id: str
    unit_id: str | None
    start: int
    end: int
    text_sha256: str
    excerpt: str = Field(max_length=20000)


def make_service(store, settings):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from .models import LocalModels
    from .retrieval.dense import DenseIndex
    from .retrieval.service import EvidenceService

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                                settings=ChromaSettings(anonymized_telemetry=False))
    return EvidenceService(store, settings.data_dir / "indexes", DenseIndex(client),
                           LocalModels(settings.model_dir))


def create_app(settings: Settings | None = None, service_factory=make_service):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir)
    app = FastAPI(title="Application Copilot Evidence", docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(32)
    app.state.store = store
    app.state.token = token
    app.state.service = None
    from .domain import contracts as domain
    from .domain import interview
    from .domain import repository as workspace
    from .domain.migrations import LegacyImporter
    repository = workspace.DomainRepository(store)
    app.state.repository = repository
    importer = LegacyImporter(repository)

    def service():
        if app.state.service is None:
            try:
                candidate = service_factory(store, settings)
                candidate.recover()
                app.state.service = candidate
            except EvidenceError:
                raise
            except Exception:
                raise EvidenceError("INDEX_NOT_READY", "Chroma is unavailable. Start the private database.", 503) from None
        return app.state.service

    def recover_cleanup():
        tickets = store.pending_cleanup()
        if tickets:
            for ticket in tickets:
                service().cleanup(ticket)
        return tickets

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}
        if host not in allowed:
            return JSONResponse({"error": {"code": "INVALID_HOST", "message": "Use the local address."}}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://{item}" for item in allowed}:
            return JSONResponse({"error": {"code": "INVALID_ORIGIN", "message": "Cross-origin requests are disabled."}}, 403)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-evidence-token", ""), token):
                return JSONResponse({"error": {"code": "INVALID_TOKEN", "message": "Reload the workbench."}}, 403)
            maximum = (10 * 1024 * 1024 + 65536 if request.url.path.endswith("/sources")
                       else 1024 * 1024 + 65536 if request.url.path in {
                           '/api/workspace/import/legacy/dry-run', '/api/workspace/import/legacy/commit'}
                       else 131072)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > maximum:
                    return JSONResponse({"error": {"code": "LIMIT_EXCEEDED", "message": "Request is too large."}}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        })
        return response

    @app.exception_handler(EvidenceError)
    async def evidence_error(_request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.safe_message}}, error.http_status)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "Check the supplied fields."}}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(_request, _error):
        return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "Operation failed; no partial success is claimed."}}, 500)

    @app.get("/health")
    def health():
        return {"status": "alive", "mode": "quote_only"}

    @app.get("/api/evidence/status")
    def status():
        with data_lock(settings.data_dir):
            from .models import LocalModels
            models_ready = LocalModels(settings.model_dir).ready
            pending = len(store.pending_cleanup())
            return {"token": token, "mode": "quote_only", "models_ready": models_ready,
                    "pending_cleanup": pending, "cloud_enabled": False,
                    "setup": "python -m copilot.models setup", "indexing": "python -m copilot index --profile ID --corpus facts|documents"}

    @app.get("/api/evidence/profiles")
    def profiles():
        with data_lock(settings.data_dir):
            return [asdict(item) for item in store.list_profiles()]

    @app.post("/api/evidence/profiles")
    def create_profile(body: ProfileInput):
        with data_lock(settings.data_dir):
            return asdict(store.create_profile(body.name, body.sectors))

    @app.get("/api/evidence/profiles/{profile_id}/sources")
    def sources(profile_id: str):
        with data_lock(settings.data_dir):
            return [asdict(item) for item in store.list_sources(profile_id)]

    @app.post("/api/evidence/profiles/{profile_id}/sources")
    def upload(profile_id: str, file: UploadFile = File(...)):
        with data_lock(settings.data_dir):
            return asdict(ingest(store, profile_id, file.filename or "", file.content_type or "", file.file.read(10 * 1024 * 1024 + 1)))

    @app.get("/api/evidence/profiles/{profile_id}/sources/{source_id}/units")
    def units(profile_id: str, source_id: str):
        with data_lock(settings.data_dir):
            return [asdict(item) for item in store.list_units(profile_id, source_id)]

    @app.get("/api/evidence/profiles/{profile_id}/sources/{source_id}/original")
    def original(profile_id: str, source_id: str):
        from starlette.responses import Response
        with data_lock(settings.data_dir):
            content = store.read_source(profile_id, source_id)
            return Response(content, media_type="application/octet-stream",
                            headers={"Content-Disposition": 'attachment; filename="source.bin"'})

    @app.get("/api/evidence/profiles/{profile_id}/facts")
    def facts(profile_id: str):
        with data_lock(settings.data_dir):
            return [asdict(item) for item in store.list_facts(profile_id)]

    @app.post("/api/evidence/profiles/{profile_id}/facts")
    def confirm(profile_id: str, body: FactInput):
        if body.confirmed is not True:
            raise EvidenceError("INVALID_INPUT", "Explicit confirmation is required.")
        try:
            origin = Span(**body.origin) if body.origin is not None else None
        except TypeError:
            raise EvidenceError("INVALID_INPUT", "Invalid origin span.") from None
        with data_lock(settings.data_dir):
            return asdict(store.confirm_fact(profile_id, body.text, origin, body.supersedes))

    @app.post("/api/evidence/profiles/{profile_id}/search")
    def search(profile_id: str, body: SearchInput):
        with data_lock(settings.data_dir):
            recover_cleanup()
            return {"mode": "quotation_not_answer", "hits": [asdict(hit) for hit in service().search(
                profile_id, body.corpus, body.query, body.source_ids, body.limit)]}

    @app.post("/api/evidence/profiles/{profile_id}/citations")
    def citation(profile_id: str, body: CitationInput):
        with data_lock(settings.data_dir):
            return store.resolve_citation(profile_id, Citation(profile_id=profile_id, **body.model_dump()))

    def finish_delete(ticket):
        try:
            service().cleanup(ticket)
            return {"ticket_id": ticket.id, "state": "complete", "scope": "application_level_not_forensic"}
        except Exception:
            return JSONResponse({"ticket_id": ticket.id, "state": "pending", "scope": "access_revoked_cleanup_pending"}, 202)

    @app.delete("/api/evidence/profiles/{profile_id}/facts/{fact_id}")
    def revoke(profile_id: str, fact_id: str):
        with data_lock(settings.data_dir):
            return finish_delete(store.revoke_fact(profile_id, fact_id))

    @app.delete("/api/evidence/profiles/{profile_id}/sources/{source_id}")
    def delete_source(profile_id: str, source_id: str):
        with data_lock(settings.data_dir):
            return finish_delete(store.delete_source(profile_id, source_id))

    @app.delete("/api/evidence/profiles/{profile_id}")
    def delete_profile(profile_id: str):
        with data_lock(settings.data_dir):
            return finish_delete(store.delete_profile(profile_id))

    async def workspace_input(request, model):
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise EvidenceError('INVALID_INPUT', 'Workspace mutations require application/json', 415)
        # Strict models intentionally use JSON validation for tuple/time wire types.
        return model.model_validate_json(await request.body())

    @app.get('/api/workspace/status')
    def workspace_status():
        return domain.WorkspaceStatus(boot_token=token, capabilities=domain.WorkspaceCapabilities(
            profile_management=True, interview=True, proposals=True, application_management=True))

    @app.get('/api/workspace/profiles')
    def workspace_profiles():
        with data_lock(settings.data_dir):
            return repository.profiles()

    @app.post('/api/workspace/profiles')
    async def workspace_create(request: Request):
        body = await workspace_input(request, domain.ProfileCreate)
        with data_lock(settings.data_dir):
            return repository.create_profile(body)

    @app.get('/api/workspace/profiles/{profile_id}')
    def workspace_detail(profile_id: str):
        with data_lock(settings.data_dir):
            return repository.detail(profile_id)

    @app.patch('/api/workspace/profiles/{profile_id}')
    async def workspace_patch(profile_id: str, request: Request):
        body = await workspace_input(request, domain.ProfilePatch)
        with data_lock(settings.data_dir):
            return repository.patch_profile(profile_id, body)

    @app.delete('/api/workspace/profiles/{profile_id}')
    def workspace_delete(profile_id: str):
        with data_lock(settings.data_dir):
            result = finish_delete(store.delete_profile(profile_id))
            pending = isinstance(result, JSONResponse)
            return domain.DeleteResult(deleted=True, cleanup_pending=pending)

    @app.get('/api/workspace/profiles/{profile_id}/export')
    def workspace_export(profile_id: str):
        with data_lock(settings.data_dir):
            return repository.export(profile_id)

    @app.post('/api/workspace/profiles/{profile_id}/consent')
    async def workspace_consent(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.ConsentInput)
        with data_lock(settings.data_dir):
            return repository.consent(profile_id, body)

    @app.get('/api/workspace/profiles/{profile_id}/facts')
    def workspace_facts(profile_id: str):
        with data_lock(settings.data_dir):
            return repository.facts(profile_id)

    @app.post('/api/workspace/profiles/{profile_id}/proposals')
    async def workspace_propose(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.ProposalInput)
        with data_lock(settings.data_dir):
            return repository.propose(profile_id, body)

    @app.post('/api/workspace/profiles/{profile_id}/proposals/{proposal_id}/review')
    async def workspace_review(profile_id: str, proposal_id: str, request: Request):
        body = await workspace_input(request, workspace.ReviewInput)
        with data_lock(settings.data_dir):
            return repository.review(profile_id, proposal_id, body)

    @app.delete('/api/workspace/profiles/{profile_id}/proposals/{proposal_id}')
    async def workspace_proposal_delete(profile_id: str, proposal_id: str, request: Request):
        body = await workspace_input(request, workspace.ProposalDelete)
        with data_lock(settings.data_dir):
            return repository.delete_proposal(profile_id, proposal_id, body)

    @app.get('/api/workspace/profiles/{profile_id}/interview')
    def workspace_questions(profile_id: str):
        with data_lock(settings.data_dir):
            detail = repository.detail(profile_id)
            return {'schema_version': 1, 'questions': interview.questions(detail.profile.sectors),
                    'progress': detail.interview, 'answers': detail.interview_answers}

    @app.post('/api/workspace/profiles/{profile_id}/interview/answers')
    async def workspace_answer(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.InterviewInput)
        with data_lock(settings.data_dir):
            return repository.answer(profile_id, body)

    @app.patch('/api/workspace/profiles/{profile_id}/interview')
    async def workspace_progress(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.ProgressInput)
        with data_lock(settings.data_dir):
            return repository.progress(profile_id, body)

    @app.post('/api/workspace/profiles/{profile_id}/typed-values')
    async def workspace_typed(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.TypedInput)
        with data_lock(settings.data_dir):
            return repository.typed(profile_id, body)

    @app.delete('/api/workspace/profiles/{profile_id}/typed-values/{record_id}')
    async def workspace_delete_typed(profile_id: str, record_id: str, request: Request):
        body = await workspace_input(request, workspace.ExpectedMetadata)
        with data_lock(settings.data_dir):
            return repository.delete_typed(profile_id, record_id, body)

    @app.post('/api/workspace/profiles/{profile_id}/applications')
    async def workspace_application_create(profile_id: str, request: Request):
        body = await workspace_input(request, workspace.ApplicationInput)
        with data_lock(settings.data_dir):
            return repository.create_application(profile_id, body)

    @app.get('/api/workspace/profiles/{profile_id}/applications/{application_id}')
    def workspace_application(profile_id: str, application_id: str):
        with data_lock(settings.data_dir):
            return repository.application(profile_id, application_id)

    @app.patch('/api/workspace/profiles/{profile_id}/applications/{application_id}')
    async def workspace_application_patch(profile_id: str, application_id: str, request: Request):
        body = await workspace_input(request, workspace.ApplicationPatch)
        with data_lock(settings.data_dir):
            return repository.patch_application(profile_id, application_id, body)

    @app.delete('/api/workspace/profiles/{profile_id}/applications/{application_id}')
    async def workspace_application_delete(profile_id: str, application_id: str, request: Request):
        body = await workspace_input(request, workspace.ApplicationPatch)
        if body.model_fields_set - {'schema_version', 'expected_input_revision', 'expected_output_revision'}:
            raise EvidenceError('INVALID_INPUT', 'Deletion accepts only expected revisions')
        with data_lock(settings.data_dir):
            return repository.delete_application(profile_id, application_id, body)

    @app.get('/api/workspace/profiles/{profile_id}/applications/{application_id}/history')
    def workspace_history(profile_id: str, application_id: str):
        with data_lock(settings.data_dir):
            return repository.history(profile_id, application_id)

    @app.post('/api/workspace/profiles/{profile_id}/applications/{application_id}/history')
    async def workspace_feedback(profile_id: str, application_id: str, request: Request):
        body = await workspace_input(request, workspace.FeedbackInput)
        with data_lock(settings.data_dir):
            return repository.feedback(profile_id, application_id, body)

    @app.post('/api/workspace/import/legacy/dry-run')
    def workspace_legacy_dry_run(file: UploadFile = File(...)):
        selected = file.file.read(1024 * 1024 + 1)
        with data_lock(settings.data_dir):
            return importer.run(selected)

    @app.post('/api/workspace/import/legacy/commit')
    def workspace_legacy_commit(request: Request, file: UploadFile = File(...)):
        # Explicit exact-byte consent: send the hash returned by a prior dry-run.
        if request.headers.get('x-confirm-legacy-import') != 'true':
            raise EvidenceError('INVALID_INPUT', 'Explicit legacy import confirmation required')
        selected = file.file.read(1024 * 1024 + 1)
        with data_lock(settings.data_dir):
            return importer.run(selected, commit=True,
                                expected_sha256=request.headers.get('x-legacy-source-sha256'))

    ui = Path(__file__).resolve().parent.parent / "ui"

    @app.get("/")
    def index():
        return FileResponse(ui / "index.html")

    app.mount("/ui", StaticFiles(directory=ui), name="ui")
    return app
