"""Pure, non-authoritative Greenhouse detail candidates; no acquisition or publication.

Required fields and bounds are local policy, not a vendor schema guarantee. Content
is literal HTML/entity text. Neither supplied bytes nor URL metadata prove a fetch,
currentness, ownership, or availability. GET success cannot establish Live status.
"""
import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from urllib.parse import parse_qsl, urlsplit

MAX_BYTES = 256 * 1024
MAX_STRING = 128 * 1024
MAX_DEPTH = 16
MAX_INTEGER = 2**63 - 1
PARSER_VERSION = 'greenhouse-v1-docs2026-10-10-localcontract'
_TOKEN = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
_HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_FIELDS = ('id', 'internal_job_id', 'title', 'company_name', 'content', 'absolute_url',
           'updated_at')


class GreenhouseError(ValueError):
    """Unsupported or malformed local input; not a statement of vendor availability."""


def _text(value: object, name: str, *, limit: int = MAX_STRING) -> str:
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise GreenhouseError(f'Invalid {name}')
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise GreenhouseError(f'Invalid {name}; positive integer required')
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise GreenhouseError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise GreenhouseError('Nonfinite JSON number')


def _bounded(value: object, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise GreenhouseError('JSON depth limit exceeded')
    if isinstance(value, str):
        if len(value) > MAX_STRING or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise GreenhouseError('JSON string limit or Unicode violation')
    elif isinstance(value, dict):
        for key, item in value.items():
            _bounded(key, depth + 1)
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1)
    elif type(value) is int and abs(value) > MAX_INTEGER:
        raise GreenhouseError('JSON integer limit exceeded')
    elif isinstance(value, float) and not math.isfinite(value):
        raise GreenhouseError('Nonfinite JSON number')


def _https(url: object):
    value = _text(url, 'URL', limit=4096)
    if (any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value
            or '#' in value or re.search(r'%(?![0-9A-Fa-f]{2})', value)):
        raise GreenhouseError('Malformed anonymous HTTPS URL')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname
                or len(parsed.hostname) > 253
                or any(not _HOST_LABEL.fullmatch(label) for label in parsed.hostname.split('.'))
                or parsed.netloc.lower() != parsed.hostname or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise GreenhouseError('Malformed anonymous HTTPS URL') from None
    return parsed


@dataclass(frozen=True)
class FieldSpan:
    field: str
    source_id: str
    unit_id: str
    canonical_sha256: str
    start: int
    end: int
    excerpt: str


@dataclass(frozen=True)
class GreenhouseCandidate:
    """Immutable evidence candidate, never an EmployerSource or generation binding."""
    source_id: str
    unit_id: str
    expected_company: str
    expected_title: str
    board_token: str
    post_id: int
    requested_url: str
    final_url: str
    original_bytes: bytes
    original_sha256: str
    canonical_text: str
    canonical_sha256: str
    spans: tuple[FieldSpan, ...]
    company_matches: bool
    title_matches: bool
    url_identity_matches: bool
    identity_matches: bool
    gaps: tuple[str, ...]
    role_state: str = 'unknown'
    eligible: bool = False
    parser_version: str = PARSER_VERSION

    def revalidate(self) -> None:
        """Reject altered fields, hashes, spans, flags, and associations."""
        # Dataclass equality alone accepts 1 == True and 4.0 == 4. Validate
        # exact scalar/container types, including nested spans, before rebuilding.
        if type(self) is not GreenhouseCandidate:
            raise GreenhouseError('Invalid candidate type')
        for field in fields(GreenhouseCandidate):
            required_type = tuple if field.name in ('spans', 'gaps') else field.type
            if type(getattr(self, field.name)) is not required_type:
                raise GreenhouseError('Invalid candidate field type')
        for span in self.spans:
            if type(span) is not FieldSpan or any(
                type(getattr(span, field.name)) is not field.type for field in fields(FieldSpan)
            ):
                raise GreenhouseError('Invalid span field type')
        if any(type(gap) is not str for gap in self.gaps):
            raise GreenhouseError('Invalid gap type')
        rebuilt = parse_greenhouse_detail(
            self.original_bytes, expected_company=self.expected_company,
            expected_title=self.expected_title, board_token=self.board_token,
            post_id=self.post_id, requested_url=self.requested_url,
            final_url=self.final_url, source_id=self.source_id, unit_id=self.unit_id,
        )
        if self != rebuilt:
            raise GreenhouseError('Candidate integrity mismatch')


def _url_identity(url: str, token: str, post: int) -> tuple[bool, tuple[str, ...]]:
    try:
        parsed = _https(url)
        identifiers = [value for key, value in parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True,
            encoding='utf-8', errors='strict', max_num_fields=100,
        ) if key == 'gh_jid']
    except (ValueError, UnicodeError):
        return False, ('absolute_url_malformed_or_ambiguous',)
    if len(identifiers) > 1:
        return False, ('absolute_url_duplicate_post_identifiers',)
    if identifiers and identifiers != [str(post)]:
        return False, ('absolute_url_post_conflict',)
    if parsed.hostname in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        match = re.fullmatch(r'/([A-Za-z0-9_-]+)/jobs/([1-9][0-9]*)/?', parsed.path)
        if match is None:
            return False, ('absolute_url_greenhouse_path_unverified',)
        if match.groups() != (token, str(post)):
            return False, ('absolute_url_board_or_post_conflict',)
        return True, ()
    # A gh_jid match on a custom site is not proof of board membership or redirects.
    return False, ('absolute_url_custom_identity_unverified',)


def parse_greenhouse_detail(
    original_bytes: bytes, *, expected_company: str, expected_title: str,
    board_token: str, post_id: int, requested_url: str, final_url: str,
    source_id: str, unit_id: str,
) -> GreenhouseCandidate:
    """Parse the local supported subset; never infer accepting applications.

    Schema/prospect/post/API-URL failures raise GreenhouseError. Exact company/title
    mismatches and ambiguous public URLs remain visible candidate gaps. Byte hashes
    cover supplied originals; canonical hashes cover derived UTF-8 selected fields.
    Spans use Python codepoint offsets, constructed alongside fields, not searches.
    """
    expected_company = _text(expected_company, 'expected company')
    expected_title = _text(expected_title, 'expected title')
    source_id = _text(source_id, 'source ID', limit=200)
    unit_id = _text(unit_id, 'unit ID', limit=200)
    if type(board_token) is not str or not _TOKEN.fullmatch(board_token):
        raise GreenhouseError('Invalid board token')
    post_id = _integer(post_id, 'expected post ID')
    expected_url = f'https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{post_id}'
    for url in (requested_url, final_url):
        _https(url)
        if url != expected_url:
            raise GreenhouseError('API URL must match exact expected board/post; no redirects')
    if type(original_bytes) is not bytes or not 0 < len(original_bytes) <= MAX_BYTES:
        raise GreenhouseError('Original byte limit exceeded')
    try:
        body = json.loads(original_bytes.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=_constant)
        _bounded(body)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GreenhouseError('Invalid bounded UTF-8 JSON') from exc
    if not isinstance(body, dict):
        raise GreenhouseError('Detail must be a JSON object')
    if _integer(body.get('id'), 'response post ID') != post_id:
        raise GreenhouseError('Response post ID conflict')
    if body.get('internal_job_id') is None:
        raise GreenhouseError('Prospect or missing internal job ID unsupported')
    _integer(body['internal_job_id'], 'internal job ID')
    for field in _FIELDS[2:]:
        _text(body.get(field), field, limit=4096 if field == 'absolute_url' else MAX_STRING)
    url_matches, url_gaps = _url_identity(body['absolute_url'], board_token, post_id)
    company_matches = body['company_name'] == expected_company
    title_matches = body['title'] == expected_title
    gaps = list(url_gaps)
    if not company_matches:
        gaps.append('company_mismatch')
    if not title_matches:
        gaps.append('title_mismatch')
    gaps.append('availability_unknown_get_does_not_prove_live')
    parts = []
    offsets = []
    cursor = 0
    for field in _FIELDS:
        value = str(body[field])
        prefix = f'{field}: '
        start = cursor + len(prefix)
        parts.append(prefix + value + '\n')
        offsets.append((field, start, start + len(value), value))
        cursor = start + len(value) + 1
    canonical = ''.join(parts)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    spans = tuple(FieldSpan(field, source_id, unit_id, digest, start, end, value)
                  for field, start, end, value in offsets)
    return GreenhouseCandidate(
        source_id=source_id, unit_id=unit_id, expected_company=expected_company,
        expected_title=expected_title, board_token=board_token, post_id=post_id,
        requested_url=requested_url, final_url=final_url, original_bytes=original_bytes,
        original_sha256=hashlib.sha256(original_bytes).hexdigest(),
        canonical_text=canonical, canonical_sha256=digest, spans=spans,
        company_matches=company_matches, title_matches=title_matches,
        url_identity_matches=url_matches,
        identity_matches=company_matches and title_matches and url_matches,
        gaps=tuple(gaps),
    )
