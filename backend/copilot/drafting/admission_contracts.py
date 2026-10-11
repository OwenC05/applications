"""Dormant launch captures; validation and hashes are not admission authority."""
from typing import Annotated

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from ..domain.contracts import Contract, Identifier, canonical_hash
from .assessment_contracts import AssessmentRequest
from .runtime_contracts import DraftRequest

AdmissionRevision = Annotated[int, Field(ge=0, le=9007199254740991)]
_IDENTIFIER = TypeAdapter(Identifier, config={'strict': True})


def _validation_input(value):
    """Revalidate instances too, including unchecked copies and their nested fields.

    model_dump can omit unknown or missing attributes on forged instances. Reading
    their stored values instead preserves those errors for strict schema validation.
    """
    if isinstance(value, BaseModel):
        values = dict(value.__dict__)
        values.update(value.__pydantic_extra__ or {})
        return {key: _validation_input(item) for key, item in values.items()}
    if isinstance(value, dict):
        return {key: _validation_input(item) for key, item in value.items()}
    return value


class DraftLaunch(Contract):
    admission_revision: AdmissionRevision
    request: DraftRequest

    @field_validator('request', mode='before')
    @classmethod
    def revalidate_request(cls, value):
        return _validation_input(value)


class AssessmentLaunch(Contract):
    admission_revision: AdmissionRevision
    request: AssessmentRequest

    @field_validator('request', mode='before')
    @classmethod
    def revalidate_request(cls, value):
        return _validation_input(value)


def draft_launch_hash(owner, application, launch):
    capture = DraftLaunch.model_validate(_validation_input(launch))
    return canonical_hash(dict(operation='draft_generation_launch',
        profile_id=_IDENTIFIER.validate_python(owner),
        application_id=_IDENTIFIER.validate_python(application),
        launch=capture.model_dump(mode='json')))


def assessment_launch_hash(owner, application, parent, launch):
    capture = AssessmentLaunch.model_validate(_validation_input(launch))
    return canonical_hash(dict(operation='draft_assessment_launch',
        profile_id=_IDENTIFIER.validate_python(owner),
        application_id=_IDENTIFIER.validate_python(application),
        parent_draft_id=_IDENTIFIER.validate_python(parent),
        launch=capture.model_dump(mode='json')))
