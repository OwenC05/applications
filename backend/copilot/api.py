"""Quote-only evidence API, separate from the legacy application workflow."""
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import Settings, data_lock
from .contracts import Citation, EvidenceError, Span
from .documents import ingest
from .store import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileInput(Input):
    name: str = Field(min_length=1, max_length=100)
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
            maximum = 10 * 1024 * 1024 + 65536 if request.url.path.endswith("/sources") else 131072
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

    ui = Path(__file__).resolve().parent.parent / "ui"

    @app.get("/")
    def index():
        return FileResponse(ui / "index.html")

    app.mount("/ui", StaticFiles(directory=ui), name="ui")
    return app
