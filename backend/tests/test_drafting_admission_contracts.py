"""Dormant launch captures: validation and hashing confer no admission authority."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from copilot.drafting.admission_contracts import (
    AssessmentLaunch,
    DraftLaunch,
    assessment_launch_hash,
    draft_launch_hash,
)
from copilot.drafting.assessment_contracts import AssessmentRequest, assessment_request_hash
from copilot.drafting.runtime_contracts import DraftRequest, draft_request_hash

GOLDENS = json.loads((Path(__file__).parent / 'fixtures/admission_request_goldens.json').read_text())
MAX_REVISION = 9007199254740991
CASES = [('generation', DraftLaunch, DraftRequest), ('assessment', AssessmentLaunch, AssessmentRequest)]


def launch_hash(kind, launch, **scope_changes):
    scope = GOLDENS['scope'] | scope_changes
    if kind == 'generation':
        return draft_launch_hash(scope['owner'], scope['application'], launch)
    return assessment_launch_hash(scope['owner'], scope['application'], scope['parent'], launch)


@pytest.mark.parametrize('kind,model,request_model', CASES)
def test_independent_goldens_and_unchanged_inner_hashes(kind, model, request_model):
    data = GOLDENS[kind]
    launch = model.model_validate(data['launch'])
    assert model.model_validate(data['launch'] | {
        'request': request_model.model_validate(data['request'])}) == launch
    assert launch.model_dump(mode='json') == data['launch']
    assert model.model_validate_json(launch.model_dump_json()) == launch
    assert launch_hash(kind, launch) == data['launch_sha256']
    scope = GOLDENS['scope']
    request = request_model.model_validate(data['request'])
    if kind == 'generation':
        digest = draft_request_hash(scope['owner'], scope['application'], request)
    else:
        digest = assessment_request_hash(scope['owner'], scope['application'], scope['parent'], request)
    assert digest == data['request_sha256']
    with pytest.raises(ValidationError):
        launch.admission_revision = 9


@pytest.mark.parametrize('kind,model,_', CASES)
@pytest.mark.parametrize('revision', [0, MAX_REVISION])
def test_revision_endpoints(kind, model, _, revision):
    launch = model.model_validate(GOLDENS[kind]['launch'] | {'admission_revision': revision})
    assert launch.admission_revision == revision
    assert launch_hash(kind, launch)


@pytest.mark.parametrize('kind,model,_', CASES)
@pytest.mark.parametrize('field,value', [
    ('admission_revision', True), ('admission_revision', '3'), ('admission_revision', 3.0),
    ('admission_revision', -1), ('admission_revision', MAX_REVISION + 1),
    ('schema_version', True), ('schema_version', '1'), ('schema_version', 1.0),
    ('schema_version', 2), ('extra', 'not allowed'),
])
def test_closed_strict_outer_capture(kind, model, _, field, value):
    with pytest.raises(ValidationError):
        model.model_validate(GOLDENS[kind]['launch'] | {field: value})


@pytest.mark.parametrize('kind,model,_', CASES)
@pytest.mark.parametrize('field', ['admission_revision', 'request'])
def test_required_outer_fields(kind, model, _, field):
    data = copy.deepcopy(GOLDENS[kind]['launch'])
    del data[field]
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize('kind,model,request_model', CASES)
@pytest.mark.parametrize('change', [
    {'acknowledged': False}, {'acknowledged': 1}, {'schema_version': True},
    {'max_output_tokens': 8001},
    {'model': 'invalid model'}, {'extra': 'not allowed'},
    {'expected_revisions': {'facts': 2}},
])
def test_nested_wire_and_unchecked_typed_requests_revalidated(kind, model, request_model, change):
    data = GOLDENS[kind]['launch']
    invalid = data['request'] | change
    with pytest.raises(ValidationError):
        model.model_validate(data | {'request': invalid})
    typed = request_model.model_validate(data['request']).model_copy(update=change)
    with pytest.raises(ValidationError):
        model.model_validate(data | {'request': typed})
    constructed = request_model.model_construct(**invalid)
    # model_construct drops forbidden extras itself; retain the forged attribute
    # explicitly so the hash boundary must reject it rather than serialize it away.
    if 'extra' in change:
        constructed.__dict__['extra'] = change['extra']
    for unchecked in (typed, constructed):
        forged = model.model_construct(admission_revision=3, request=unchecked)
        with pytest.raises(ValidationError):
            launch_hash(kind, forged)


@pytest.mark.parametrize('kind,model,request_model', CASES)
def test_hash_boundary_revalidates_outer_and_deep_vector_tampering(kind, model, request_model):
    launch = model.model_validate(GOLDENS[kind]['launch'])
    for change in ({'admission_revision': True}, {'schema_version': True}, {'extra': 'bad'}):
        with pytest.raises(ValidationError):
            launch_hash(kind, launch.model_copy(update=change))
    request = request_model.model_validate(GOLDENS[kind]['request'])
    bad_vector = request.expected_revisions.model_copy(update={'research': '7'})
    bad_request = request.model_copy(update={'expected_revisions': bad_vector})
    with pytest.raises(ValidationError):
        model.model_validate(GOLDENS[kind]['launch'] | {'request': bad_request})
    with pytest.raises(ValidationError):
        launch_hash(kind, launch.model_copy(update={'request': bad_request}))
    incomplete = copy.deepcopy(request.expected_revisions)
    del incomplete.__dict__['research']
    with pytest.raises(ValidationError):
        launch_hash(kind, launch.model_copy(update={
            'request': request.model_copy(update={'expected_revisions': incomplete})}))


@pytest.mark.parametrize('kind,model,_', CASES)
def test_hash_binds_capture_request_and_scope_without_unicode_normalization(kind, model, _):
    launch = model.model_validate(GOLDENS[kind]['launch'])
    original = launch_hash(kind, launch)
    assert launch_hash(kind, launch.model_copy(update={'admission_revision': 4})) != original
    changed_request = launch.request.model_copy(update={'max_output_tokens': 2001})
    assert launch_hash(kind, launch.model_copy(update={'request': changed_request})) != original
    decomposed = launch.request.idempotency_key.replace('é', 'e\u0301')
    assert launch_hash(kind, launch.model_copy(update={
        'request': launch.request.model_copy(update={'idempotency_key': decomposed})})) != original
    for field in ('owner', 'application') + (('parent',) if kind == 'assessment' else ()):
        assert launch_hash(kind, launch, **{field: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'}) != original
        for invalid in ('not-a-uuid', True, None):
            with pytest.raises(ValidationError):
                launch_hash(kind, launch, **{field: invalid})
    assert GOLDENS['generation']['launch_sha256'] != GOLDENS['assessment']['launch_sha256']


def test_import_validation_and_hashing_do_not_initialize_runtime_or_perform_io():
    script = r"""
import importlib.abc
import json
import socket
import sqlite3
import sys
from pathlib import Path

def forbidden(*args, **kwargs):
    raise AssertionError('Runtime IO is forbidden for dormant contracts')

sqlite3.connect = forbidden
socket.socket.connect = forbidden
socket.create_connection = forbidden

class NoRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'chromadb', 'transformers', 'sentence_transformers', 'torch'} or fullname in {
            'copilot.store', 'copilot.provider', 'copilot.jobs', 'copilot.runtime',
            'copilot.domain.repository', 'copilot.drafting.service', 'copilot.drafting.worker',
        }:
            raise AssertionError('Runtime import is forbidden: ' + fullname)

sys.meta_path.insert(0, NoRuntimeImports())
from copilot.drafting.admission_contracts import (
    DraftLaunch, AssessmentLaunch, draft_launch_hash, assessment_launch_hash,
)
data = json.loads(Path(sys.argv[1]).read_text())
scope = data['scope']
assert draft_launch_hash(scope['owner'], scope['application'],
    DraftLaunch.model_validate(data['generation']['launch'])) == data['generation']['launch_sha256']
assert assessment_launch_hash(scope['owner'], scope['application'], scope['parent'],
    AssessmentLaunch.model_validate(data['assessment']['launch'])) == data['assessment']['launch_sha256']
"""
    result = subprocess.run([sys.executable, '-c', script,
        str(Path(__file__).parent / 'fixtures/admission_request_goldens.json')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_operation_specific_helpers_reject_the_other_request_schema():
    scope = GOLDENS['scope']
    generation = DraftLaunch.model_validate(GOLDENS['generation']['launch'])
    assessment = AssessmentLaunch.model_validate(GOLDENS['assessment']['launch'])
    with pytest.raises(ValidationError):
        draft_launch_hash(scope['owner'], scope['application'], assessment)
    with pytest.raises(ValidationError):
        assessment_launch_hash(scope['owner'], scope['application'], scope['parent'], generation)
