"""Frozen evidence boundary: Unicode half-open spans and canonical SHA-256 hashes."""
from dataclasses import dataclass
from typing import Literal

Corpus = Literal["facts", "documents", "employer"]


class EvidenceError(Exception):
    def __init__(self, code: str, safe_message: str, http_status: int = 400):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.http_status = http_status


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    sectors: list[str]
    revision: int


@dataclass(frozen=True)
class Source:
    id: str
    profile_id: str
    filename: str
    media_type: str
    blob_sha256: str
    parser_version: str
    state: str = "active"


@dataclass(frozen=True)
class Unit:
    id: str
    profile_id: str
    source_id: str
    ordinal: int
    page: int | None
    text: str
    text_sha256: str


@dataclass(frozen=True)
class Span:
    unit_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Fact:
    id: str
    profile_id: str
    text: str
    text_sha256: str
    confirmed_at: str
    provenance: str
    origin: Span | None = None
    state: str = "active"


@dataclass(frozen=True)
class Chunk:
    id: str
    profile_id: str
    corpus: Corpus
    record_id: str
    unit_id: str | None
    start: int
    end: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class Snapshot:
    profile_id: str
    corpus: Corpus
    revision: int
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class Manifest:
    generation_id: str
    profile_id: str
    corpus: Corpus
    revision: int
    model_fingerprint: str
    chunker_version: str
    chunk_ids_sha256: str
    chunk_count: int
    dense_collection: str
    sparse_relpath: str
    state: str = "active"


@dataclass(frozen=True)
class Citation:
    profile_id: str
    corpus: Corpus
    record_id: str
    unit_id: str | None
    start: int
    end: int
    text_sha256: str
    excerpt: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    citation: Citation
    dense_rank: int | None
    bm25_rank: int | None
    rrf_score: float
    rerank_score: float


@dataclass(frozen=True)
class CleanupTicket:
    id: str
    profile_id: str
    generation_ids: list[str]
    source_ids: list[str]
    fact_ids: list[str]
    state: str = "pending"


# Scoped projections deliberately leave every personal positional constructor intact.
@dataclass(frozen=True, kw_only=True)
class EmployerUnit(Unit):
    application_id: str
    research_run_id: str

    def __post_init__(self):
        _validate_employer_projection(self)


@dataclass(frozen=True, kw_only=True)
class EmployerChunk(Chunk):
    application_id: str
    research_run_id: str

    def __post_init__(self):
        _validate_employer_projection(self)


@dataclass(frozen=True, kw_only=True)
class EmployerSnapshot(Snapshot):
    application_id: str
    research_run_id: str

    def __post_init__(self):
        _validate_employer_projection(self)


@dataclass(frozen=True, kw_only=True)
class EmployerManifest(Manifest):
    application_id: str
    research_run_id: str

    def __post_init__(self):
        _validate_employer_projection(self)


@dataclass(frozen=True, kw_only=True)
class EmployerCitation(Citation):
    application_id: str
    research_run_id: str

    def __post_init__(self):
        _validate_employer_projection(self)


def _validate_employer_projection(value):
    from .retrieval.packet_contracts import CorpusScope
    if getattr(value, 'corpus', 'employer') != 'employer':
        raise ValueError('Employer projection requires employer corpus')
    CorpusScope(profile_id=value.profile_id, corpus='employer',
                application_id=value.application_id, research_run_id=value.research_run_id)


def decode_manifest(value):
    """Discriminate before construction: employer never uses a personal fallback."""
    if value.get('corpus') == 'employer':
        from .retrieval.packet_contracts import CorpusScope
        CorpusScope(profile_id=value['profile_id'], corpus='employer',
                    application_id=value.get('application_id'), research_run_id=value.get('research_run_id'))
        return EmployerManifest(**value)
    if value.get('corpus') not in ('facts', 'documents'):
        raise ValueError('Unknown manifest corpus')
    return Manifest(**value)


def decode_chunk(value):
    if value.get('corpus') == 'employer':
        from .retrieval.packet_contracts import CorpusScope
        CorpusScope(profile_id=value['profile_id'], corpus='employer',
                    application_id=value.get('application_id'), research_run_id=value.get('research_run_id'))
        return EmployerChunk(**value)
    if value.get('corpus') not in ('facts', 'documents'):
        raise ValueError('Unknown chunk corpus')
    return Chunk(**value)
