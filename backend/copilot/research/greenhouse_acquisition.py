"""Unused anonymous Greenhouse composition; observations confer no authority."""
import re
import time
from dataclasses import dataclass

from .fetch import (
    AcquiredGreenhouseJSON,
    FetchBudget,
    _acquire_greenhouse_json_until,
    _remaining,
)
from .greenhouse import (
    MAX_INTEGER,
    MAX_STRING,
    PARSER_VERSION,
    GreenhouseCandidate,
    GreenhouseError,
    parse_greenhouse_detail,
)

ADAPTER_VERSION = 'greenhouse-acquisition-v1-localcontract'


@dataclass(frozen=True)
class GreenhouseAcquisition:
    """Frozen association data, not a signature, capability or persisted source."""
    transport: AcquiredGreenhouseJSON
    candidate: GreenhouseCandidate
    adapter_version: str = ADAPTER_VERSION
    provenance: str = 'anonymous_greenhouse_detail_get'

    def __post_init__(self):
        if (type(self.transport) is not AcquiredGreenhouseJSON
                or type(self.candidate) is not GreenhouseCandidate
                or self.adapter_version != ADAPTER_VERSION
                or self.provenance != 'anonymous_greenhouse_detail_get'):
            raise GreenhouseError('Invalid acquisition association')
        self.candidate.revalidate()
        candidate, transport = self.candidate, self.transport
        if (candidate.parser_version != PARSER_VERSION
                or candidate.requested_url != transport.requested_url
                or candidate.final_url != transport.final_url
                or candidate.original_bytes != transport.decoded_json_bytes
                or candidate.original_sha256 != transport.decoded_json_sha256):
            raise GreenhouseError('Transport and parser association mismatch')


def acquire_greenhouse_detail(
    *, expected_company: str, expected_title: str, board_token: str, post_id: int,
    source_id: str, unit_id: str, confirmed_hosts: list[str], budget: FetchBudget,
) -> GreenhouseAcquisition:
    """Acquire and parse one explicit detail; no runtime or availability promotion."""
    deadline = min(time.monotonic() + 15, budget.started + budget.run_seconds)
    for value, name, limit in (
        (expected_company, 'expected company', MAX_STRING),
        (expected_title, 'expected title', MAX_STRING),
        (source_id, 'source ID', 200), (unit_id, 'unit ID', 200),
    ):
        if (type(value) is not str or not value.strip() or len(value) > limit
                or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise GreenhouseError(f'Invalid {name}')
    if type(board_token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', board_token):
        raise GreenhouseError('Invalid board token')
    if type(post_id) is not int or not 1 <= post_id <= MAX_INTEGER:
        raise GreenhouseError('Invalid expected post ID; positive integer required')
    endpoint = f'https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{post_id}'
    transport = _acquire_greenhouse_json_until(endpoint, confirmed_hosts, budget, deadline)
    candidate = parse_greenhouse_detail(
        transport.decoded_json_bytes, expected_company=expected_company,
        expected_title=expected_title, board_token=board_token, post_id=post_id,
        requested_url=transport.requested_url, final_url=transport.final_url,
        source_id=source_id, unit_id=unit_id)
    if ((candidate.source_id, candidate.unit_id, candidate.expected_company,
         candidate.expected_title, candidate.board_token, candidate.post_id)
            != (source_id, unit_id, expected_company, expected_title, board_token, post_id)):
        raise GreenhouseError('Parser returned a different acquisition association')
    result = GreenhouseAcquisition(transport, candidate)
    _remaining(deadline)
    return result
