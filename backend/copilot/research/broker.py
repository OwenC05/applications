"""Bounded anonymous static research; persistence and job authority stay external.

This deliberately narrow static adapter recognizes exact, unique labeled identity
lines, not generic employer/title keyword co-occurrence. Unsupported page layouts
remain incomplete. Labels prove only displayed identity, never truth/entailment of
employer claims. Original source text is untrusted data, never agent instructions.
No renderer, search engine, model, credentials, applicant facts or user JD channel.
"""
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from ..domain.contracts import (
    ApplicationOwned,
    EmployerSource,
    ResearchRun,
    RevisionVector,
    SourceSpan,
)
from .fetch import MAX_BYTES, AcquiredSource, FetchBudget, FetchError, _host, acquire, validate_url

MAX_SOURCES = 6
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_CANONICAL_CODEPOINTS = 1_000_000


class AuthorityLost(RuntimeError):
    """Caller must discard all results; cancellation is not a partial success."""


class ResearchInput(ApplicationOwned):
    """Captured non-personal fields only; IDs/revisions are local association data."""
    revisions: RevisionVector
    company: Annotated[str, Field(min_length=1, max_length=200)]
    role: Annotated[str, Field(min_length=1, max_length=300)]
    company_url: str
    vacancy_url: str
    vacancy_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    confirmed_hosts: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]
    supporting_urls: Annotated[tuple[str, ...], Field(max_length=4)] = ()

    @field_validator('company', 'role', 'vacancy_id')
    @classmethod
    def identity_line(cls, value):
        if value is not None and (value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError('Identity must be one exact nonempty line')
        return value

    @model_validator(mode='after')
    def bounded_urls(self):
        hosts = {_host(host) for host in self.confirmed_hosts}
        if len(hosts) != len(self.confirmed_hosts):
            raise ValueError('Duplicate confirmed host')
        urls = (self.company_url, self.vacancy_url, *self.supporting_urls)
        normalized = []
        for url in urls:
            if '#' in url:
                raise ValueError('Research URL cannot contain a fragment')
            normalized.append(validate_url(url, hosts)[0])
        if len(set(normalized)) != len(normalized):
            raise ValueError('Select distinct company, role and supporting URLs')
        return self


@dataclass(frozen=True)
class SourceCapsule:
    source: EmployerSource
    unit_id: str
    original_bytes: bytes
    canonical_text: str
    media_type: str
    content_encoding: str
    identity_spans: tuple[SourceSpan, ...]


@dataclass(frozen=True)
class BrokerResult:
    run: ResearchRun
    sources: tuple[SourceCapsule, ...]


def _authority(current):
    try:
        allowed = current()
    except Exception:
        raise AuthorityLost('Research authority check failed') from None
    if allowed is not True:
        raise AuthorityLost('Research authority is no longer current')


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise FetchError('Invalid acquisition timestamp')
    return value


def _validated(acquired, requested, hosts, now):
    if not isinstance(acquired, AcquiredSource):
        raise FetchError('Invalid source capsule')
    original = validate_url(acquired.requested_url, hosts)[0]
    final = validate_url(acquired.final_url, hosts)[0]
    if original != requested or acquired.provenance != 'anonymous_static_download':
        raise FetchError('Source provenance mismatch')
    if (not isinstance(acquired.original_bytes, bytes) or len(acquired.original_bytes) > MAX_BYTES
            or not isinstance(acquired.canonical_text, str)
            or len(acquired.canonical_text) > MAX_CANONICAL_CODEPOINTS
            or hashlib.sha256(acquired.original_bytes).hexdigest() != acquired.original_sha256
            or hashlib.sha256(acquired.canonical_text.encode('utf-8')).hexdigest()
            != acquired.canonical_sha256):
        raise FetchError('Source integrity mismatch')
    acquired_at = _utc(datetime.fromisoformat(acquired.acquired_at))
    if acquired_at > now:
        raise FetchError('Acquisition time is in the future')
    return final, acquired_at


def _label_span(item, label, expected=None):
    """Unique full-line value, exact case/Unicode; no substring identity guessing."""
    prefix = label + ': '
    matches = []
    offset = 0
    for line in item.canonical_text.splitlines(keepends=True):
        text = line.rstrip('\r\n')
        if text.startswith(prefix):
            value = text[len(prefix):]
            matches.append((offset + len(prefix), value))
        offset += len(line)
    if len(matches) != 1:
        return None
    start, value = matches[0]
    if not value or (expected is not None and value != expected):
        return None
    return SourceSpan(record_id=item.source.id, unit_id=item.unit_id,
                      start=start, end=start + len(value),
                      text_sha256=item.source.canonical_sha256, excerpt=value)


def _static_accessible(text):
    """Conservative lexical gate shared by company and exact-role coverage.

    Even navigation-only sign-in/JavaScript mentions can cause false negatives:
    this static adapter cannot establish which content is gated or rendered.
    Unknown layouts must not become complete through positive identity labels.
    """
    normalized = ' '.join(text.casefold().replace('-', ' ').split())
    return not any(marker in normalized for marker in (
        'sign in', 'log in', 'login', 'javascript', 'authentication required',
        'authorization required', 'access denied',
        'captcha', 'paywall', 'subscribe to view', 'subscription required',
        'verify you are human',
    ))


def _availability_consistent(text, state):
    """Reject lexical availability conflicts; never infer status from prose.

    Only the unique Applications label establishes state. Other availability
    signals cannot override it. Broad blockers deliberately prefer an incomplete
    result over guessing context or trusting a positive label amid contradictions.
    """
    remainder = ' '.join(line.casefold() for line in text.splitlines()
                         if line not in ('Applications: open', 'Applications: closed'))
    normalized = ' '.join(remainder.split())
    negative = ('closed', 'no longer available', 'no longer accepting', 'not accepting',
                'filled', 'unavailable', 'expired', 'withdrawn')
    positive = ('applications are open', 'accepting applications', 'apply now')
    if state == 'open':
        return not any(marker in normalized for marker in negative)
    if state == 'closed':
        return not any(marker in normalized for marker in positive)
    return False


def research(inputs: ResearchInput, *, current: Callable[[], bool],
             acquire_source: Callable = acquire,
             clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> BrokerResult:
    """Acquire explicit URLs only. Never publish without the caller's fenced CAS.

    The authority callback is mandatory before every acquisition, after every
    acquisition, and before return. The fetcher separately enforces per-redirect
    anonymous HTTPS/DNS budgets. Cancellation during a blocking fetch cannot undo
    its anonymous GET; it prevents accepting its result or starting further work.
    """
    if not isinstance(inputs, ResearchInput):
        raise TypeError('Validated captured research input required')
    _authority(current)
    started = _utc(clock())
    hosts = {_host(host) for host in inputs.confirmed_hosts}
    run_id = str(uuid4())
    budget = FetchBudget(max_requests=10, run_seconds=120, max_bytes=MAX_RUN_BYTES)
    sources, company_ids, role_ids, gaps = [], [], [], []
    total_bytes = 0
    closed = False
    selected = [('company', inputs.company_url), ('exact_role', inputs.vacancy_url)]
    selected.extend(('supporting', url) for url in inputs.supporting_urls)
    assert len(selected) <= MAX_SOURCES
    for purpose, url in selected:
        _authority(current)
        requested = validate_url(url, hosts)[0]
        if total_bytes + MAX_BYTES > MAX_RUN_BYTES or budget.bytes_read >= MAX_RUN_BYTES:
            gaps.append('run_source_byte_budget_exhausted')
            break
        try:
            acquired = acquire_source(requested, sorted(hosts), budget)
            _authority(current)
            final, acquired_at = _validated(acquired, requested, hosts, _utc(clock()))
            total_bytes += len(acquired.original_bytes)
            if total_bytes > MAX_RUN_BYTES:
                raise FetchError('Run source byte quota exceeded')
            source = EmployerSource(
                id=str(uuid4()), profile_id=inputs.profile_id, application_id=inputs.application_id,
                research_run_id=run_id, purpose=purpose, provenance='public_fetch',
                original_url=requested, final_url=final, acquired_at=acquired_at,
                original_sha256=acquired.original_sha256, canonical_sha256=acquired.canonical_sha256,
                extraction_version=acquired.parser_version,
                vacancy_id=inputs.vacancy_id if purpose == 'exact_role' else None)
            item = SourceCapsule(source, str(uuid4()), acquired.original_bytes,
                                 acquired.canonical_text, acquired.media_type,
                                 acquired.content_encoding, ())
            spans = []
            accessible = _static_accessible(item.canonical_text)
            if purpose == 'company':
                span = _label_span(item, 'Company', inputs.company)
                if accessible and span is not None and final == requested:
                    spans.append(span)
                    company_ids.append(source.id)
                else:
                    gaps.append('company_identity_unproven' if accessible
                                else 'company_accessibility_unproven')
            elif purpose == 'exact_role':
                employer = _label_span(item, 'Employer', inputs.company)
                title = _label_span(item, 'Role', inputs.role)
                vacancy = _label_span(item, 'Vacancy ID', inputs.vacancy_id)
                state = _label_span(item, 'Applications')
                role_state = state.excerpt if state and state.excerpt in ('open', 'closed') else 'unknown'
                consistent = _availability_consistent(item.canonical_text, role_state)
                if (accessible and consistent and final == requested and employer and title
                        and (inputs.vacancy_id is None or vacancy)):
                    spans.extend((employer, title))
                    if vacancy:
                        spans.append(vacancy)
                    source = source.model_copy(update={'role_state': role_state})
                    if state:
                        spans.append(state)
                    role_ids.append(source.id)
                    if role_state == 'closed':
                        closed = True
                        gaps.append('role_closed')
                else:
                    if not accessible:
                        gaps.append('exact_role_accessibility_unproven')
                    elif not consistent:
                        gaps.append('role_availability_unproven')
                    else:
                        gaps.append('exact_role_identity_unproven')
            sources.append(SourceCapsule(source, item.unit_id, item.original_bytes,
                                         item.canonical_text, item.media_type,
                                         item.content_encoding, tuple(spans)))
        except (FetchError, ValueError):
            # Never persist exception contents (URLs, source prose, tokens, names).
            gaps.append(purpose + '_static_source_unavailable')
            if budget.bytes_read >= MAX_RUN_BYTES:
                gaps.append('run_source_byte_budget_exhausted')
            if total_bytes > MAX_RUN_BYTES or budget.bytes_read >= MAX_RUN_BYTES:
                break
    if not company_ids:
        gaps.append('company_coverage_missing')
    if not role_ids:
        gaps.append('exact_role_coverage_missing')
    acquired_at = min((item.source.acquired_at for item in sources), default=started)
    expires_at = acquired_at + timedelta(days=7)
    if _utc(clock()) >= expires_at:
        state = 'stale'
        gaps.append('research_freshness_expired')
    elif closed:
        state = 'closed_role'
    else:
        state = 'incomplete' if gaps else 'complete'
    _authority(current)
    result = ResearchRun(profile_id=inputs.profile_id, application_id=inputs.application_id,
                         id=run_id, revisions=inputs.revisions, state=state,
                         company_source_ids=tuple(company_ids), exact_role_source_ids=tuple(role_ids),
                         acquired_at=acquired_at, expires_at=expires_at,
                         gaps=tuple(dict.fromkeys(gaps)))
    return BrokerResult(result, tuple(sources))
