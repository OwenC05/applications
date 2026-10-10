"""Explicit duplicate-charge warning acknowledgement; no client payload capability."""
from typing import Annotated, Literal

from pydantic import Field, StrictBool, field_validator

from .domain import contracts as c

WARNING_VERSION = 'duplicate_charge_possible_v1'
WARNING_TEXT = ('The previous provider attempt may already have been billed. Retrying can cause duplicate charges. '
                'Its original outcome and reserved budget remain unresolved; retrying does not reconcile them.')


class WarnedRetry(c.Contract):
    prior_attempt_id: c.Identifier
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    acknowledge_duplicate_charge: StrictBool
    warning_version: Literal['duplicate_charge_possible_v1']

    @field_validator('acknowledge_duplicate_charge')
    @classmethod
    def affirmative(cls, value):
        if value is not True:
            raise ValueError('Explicit duplicate-charge warning acknowledgement required')
        return value
