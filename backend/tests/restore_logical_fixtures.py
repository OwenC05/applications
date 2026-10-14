"""Writer-backed SYNTHETIC logical snapshots; never initialize an input database."""
import hashlib
import io
import json
import sqlite3
import tarfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from restore_stage_catalog_fixtures import expected_catalog


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@dataclass
class Installation:
    store: object
    jobs: object
    domain: object
    owner: str
    application_id: str

    @property
    def database(self):
        return self.store.db

    @property
    def originals(self):
        connection = sqlite3.connect(self.database)
        try:
            identifiers = [row[0] for row in connection.execute(
                "SELECT id FROM records WHERE kind IN ('source','workspace:employer_source')")]
        finally:
            connection.close()
        return {identifier: (self.store.blobs / identifier).read_bytes() for identifier in identifiers}


def create_installation(root, sector='tech', name='Synthetic Applicant'):
    if Path(root).exists():
        raise ValueError('Synthetic installation root must not exist')
    from copilot.domain import contracts as c
    from copilot.domain.repository import ApplicationInput, DomainRepository
    from copilot.jobs import Jobs
    from copilot.store import Store

    store = Store(root)
    jobs, domain = Jobs(store), DomainRepository(store)
    profile = domain.create_profile(c.ProfileCreate(name=name, sectors=(sector,))).profile
    app = domain.create_application(profile.profile_id, ApplicationInput(
        expected_metadata_revision=profile.revisions.metadata,
        company='Synthetic Company', role='Engineer' if sector == 'tech' else 'Analyst',
        sector=sector, vacancy_url='https://example.test/role', job_description='Synthetic role',
        questions=(c.Question(id='why', text='Why this role?', type='writing', constraint_origin='user'),)))
    return Installation(store, jobs, domain, profile.profile_id, app.application_id)


@dataclass(frozen=True)
class PersonalEvidence:
    source: object
    unit: object
    fact: object
    proposal: object


def add_personal_evidence(bundle, text='Built synthetic tools 😀\r\nwith exact evidence.'):
    from copilot.domain import contracts as c
    from copilot.domain.repository import ProposalInput, ReviewInput
    from copilot.store import digest

    source = bundle.store.add_source(bundle.owner, 'synthetic.txt', 'text/plain',
                                     text.encode('utf-8'), [text], 'utf8-newlines-v1')
    unit = bundle.store.list_units(bundle.owner, source.id)[0]
    revisions = bundle.domain.detail(bundle.owner).profile.revisions
    proposal = bundle.domain.propose(bundle.owner, ProposalInput(
        expected_metadata_revision=revisions.metadata, text=unit.text,
        source_spans=(c.SourceSpan(record_id=source.id, unit_id=unit.id, start=0,
            end=len(unit.text), text_sha256=digest(unit.text), excerpt=unit.text),)))
    revisions = bundle.domain.detail(bundle.owner).profile.revisions
    bundle.domain.review(bundle.owner, proposal.id, ReviewInput(
        expected_metadata_revision=revisions.metadata, expected_facts_revision=revisions.facts,
        action='confirm', confirmed=True))
    fact = next(value for value in bundle.store.list_facts(bundle.owner) if value.text == unit.text)
    return PersonalEvidence(source, unit, fact, proposal)


def add_workspace_rows(bundle):
    """Real preferences/consent/typed/interview/history writers, no provider actions."""
    from copilot.domain import contracts as c
    from copilot.domain import interview
    from copilot.domain.repository import ConsentInput, FeedbackInput, InterviewInput, TypedInput

    def revisions():
        return bundle.domain.detail(bundle.owner).profile.revisions

    bundle.domain.patch_profile(bundle.owner, c.ProfilePatch(
        expected_metadata_revision=revisions().metadata, writing_preferences='Direct Unicode 😀\r\n'))
    bundle.domain.consent(bundle.owner, ConsentInput(expected_consent_revision=revisions().consent,
        provider='openai', purposes=('drafting',), granted=False))
    bundle.domain.typed(bundle.owner, TypedInput(expected_metadata_revision=revisions().metadata,
        kind='contact', field='email', value='synthetic@example.test', purpose='Synthetic fixture',
        explicitly_confirmed=True))
    question = interview.questions(bundle.domain.detail(bundle.owner).profile.sectors)[0]
    for answer in ('First synthetic answer 😀\r\n', 'Revised synthetic answer 😀\r\n'):
        bundle.domain.answer(bundle.owner, InterviewInput(expected_metadata_revision=revisions().metadata,
                                                        question_id=question['id'], answer=answer))
    app = bundle.domain.application(bundle.owner, bundle.application_id)
    bundle.domain.feedback(bundle.owner, bundle.application_id, FeedbackInput(
        expected_input_revision=app.input_revision, expected_output_revision=app.output_revision,
        event_id=str(uuid4()), text='Synthetic feedback; not a submission receipt', kind='feedback'))
    return bundle


def table_columns():
    return {table: tuple(row[1] for row in metadata['table_xinfo'])
            for table, metadata in expected_catalog()['metadata'].items()}


def read_rows(bundle_or_database):
    database = bundle_or_database.database if isinstance(bundle_or_database, Installation) else Path(bundle_or_database)
    connection = sqlite3.connect(database)
    try:
        return {table: tuple(tuple(row) for row in connection.execute(
            'SELECT ' + ','.join('"' + column + '"' for column in columns) + ' FROM "' + table + '"'))
            for table, columns in table_columns().items()}
    finally:
        connection.close()


def copy_database(bundle_or_database, destination):
    source = bundle_or_database.database if isinstance(bundle_or_database, Installation) else Path(bundle_or_database)
    # Source is our own closed rollback-format fixture, not an incoming capture.
    with Path(destination).open('xb') as stream:
        stream.write(source.read_bytes())
    return Path(destination)


def mutate_row(database, table, row_key, column, value):
    columns = table_columns()
    if table not in columns or column not in columns[table] or any(key not in columns[table] for key in row_key):
        raise ValueError('Unknown trusted test table/column')
    connection = sqlite3.connect(database)
    try:
        where = ' AND '.join('"' + key + '" IS ?' for key in row_key)
        cursor = connection.execute('UPDATE "' + table + '" SET "' + column + '"=? WHERE ' + where,
                                    (value, *row_key.values()))
        if cursor.rowcount != 1:
            raise AssertionError('Mutation must select exactly one synthetic row')
        connection.commit()
    finally:
        connection.close()


def archive_bundle(bundle, target, *, database=None, originals=None, manifest_changes=None):
    database = bundle.database if database is None else Path(database)
    originals = bundle.originals if originals is None else originals
    connection = sqlite3.connect(database)
    try:
        records = {row[0]: row[1:] for row in connection.execute(
            "SELECT id,owner,kind FROM records WHERE kind IN ('source','workspace:employer_source')")}
    finally:
        connection.close()
    payloads = [('evidence.sqlite3', database.read_bytes())]
    payloads.extend((f'blobs/{identifier}', payload) for identifier, payload in sorted(originals.items()))
    entries = [{'path': path, 'type': 'file', 'size': len(payload), 'sha256': sha256(payload)}
               for path, payload in payloads]
    declarations = []
    for identifier, payload in sorted(originals.items()):
        if identifier not in records:
            raise ValueError('Explicit fabricated blob declaration requires manifest_changes, not legal fallback')
        owner, kind = records[identifier]
        declarations.append({'source_id': identifier, 'owner': owner, 'source_kind': kind,
                             'path': f'blobs/{identifier}', 'sha256': sha256(payload)})
    model_lock = Path(__file__).parents[1] / 'models.lock.json'
    manifest = {'format': 'copilot-backup', 'version': 1, 'backup_id': str(uuid4()),
        'source_schema_sha256': expected_catalog()['catalog_sha256'],
        'application_compatibility': 'application-copilot-evidence-0.2.0',
        'model_lock_sha256': sha256(model_lock.read_bytes()), 'installation_id': str(uuid4()),
        'entries': entries, 'originals': declarations,
        'snapshot': {'method': 'offline-stage-backup-v1', 'raw_capture_sha256': 'b' * 64,
                     'canonical_database_sha256': entries[0]['sha256']}}
    if manifest_changes:
        manifest_changes(manifest)
    with tarfile.open(target, mode='x', format=tarfile.USTAR_FORMAT) as writer:
        for name, payload in [('manifest.json', encode(manifest).encode('utf-8')), *payloads]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(payload), 0o600
            info.uid = info.gid = info.mtime = 0
            info.type = tarfile.REGTYPE
            writer.addfile(info, io.BytesIO(payload))
    return Path(target)


def add_legacy_rows(bundle):
    """Actual migration writer on synthetic bytes ONLY, never a validator fallback."""
    from datetime import datetime, timezone

    from copilot.domain.migrations import LegacyImporter

    owner, memory, first, latest = (str(uuid4()) for _ in range(4))
    timestamp = datetime.now(timezone.utc).isoformat()
    profile = {'id': owner, 'name': 'Synthetic legacy applicant', 'sectors': ['tech', 'finance'],
        'isDemo': False, 'cloudConsent': True, 'writingPreferences': 'Historical preference',
        'revision': 4, 'brainRevision': 2, 'createdAt': timestamp, 'updatedAt': timestamp,
        'memories': [{'id': memory, 'category': 'project', 'label': 'Synthetic legacy project',
            'content': 'Built a synthetic legacy tool', 'status': 'verified',
            'source': {'kind': 'interview', 'id': first, 'label': 'Historical answer'},
            'supersedes': None, 'createdAt': timestamp, 'confirmedAt': timestamp}],
        'interview': {'answers': [
            {'id': first, 'questionId': 'common-direction', 'question': 'Career direction',
             'section': 'Career', 'answer': 'Earlier synthetic answer', 'createdAt': timestamp},
            {'id': latest, 'questionId': 'common-direction', 'question': 'Career direction',
             'section': 'Career', 'answer': 'Latest synthetic answer', 'createdAt': timestamp}],
            'skippedQuestionIds': [], 'completed': False}, 'applications': []}
    original = encode(profile).encode('utf-8')
    result = LegacyImporter(bundle.domain).run(original, commit=True, expected_sha256=sha256(original))
    assert result['state'] == 'imported'
    return owner, original


@dataclass
class DraftingHistory:
    service: object
    scope: object
    batch: object
    draft: object
    job: object
    request: object
    sent: list
    settings: dict
    lease: object
    run: object


def add_drafting_history(bundle, batch, scope, *, run=True):
    """Actual paid-accounting writer with literal FAKE key/in-memory transport.

    No real provider activity: hard tripwires reject sockets/default HTTP, SDK
    imports and environment-key discovery throughout fixture construction/run.
    """
    import builtins
    import os
    from contextlib import ExitStack
    from unittest.mock import patch

    original_import, original_get = builtins.__import__, type(os.environ).get

    def forbidden(*args, **kwargs):
        raise AssertionError('Synthetic writer fixture attempted external runtime access')

    def checked_import(name, *args, **kwargs):
        if name.split('.')[0] in {'chromadb', 'openai', 'torch', 'sentence_transformers'}:
            forbidden()
        return original_import(name, *args, **kwargs)

    def checked_get(environment, key, default=None):
        if key == 'OPENAI_API_KEY':
            forbidden()
        return original_get(environment, key, default)

    with ExitStack() as stack:
        stack.enter_context(patch('builtins.__import__', checked_import))
        stack.enter_context(patch.object(type(os.environ), 'get', checked_get))
        stack.enter_context(patch('socket.socket', forbidden))
        from copilot.domain.repository import ConsentInput
        from copilot.drafting.admission_contracts import DraftLaunch
        from copilot.drafting.runtime_contracts import DraftPreviewRequest, DraftRequest
        from copilot.drafting.service import DraftingService
        from copilot.provider import Limits, Provider

        stack.enter_context(patch('copilot.provider.ResponsesHTTP.__call__', forbidden))
        stack.enter_context(patch('copilot.provider.ProviderHTTPS', forbidden))
        current = bundle.domain.detail(bundle.owner).profile.revisions
        bundle.domain.consent(bundle.owner, ConsentInput(expected_consent_revision=current.consent,
            provider='openai', purposes=('research', 'drafting', 'assessment'), granted=True))
        sent = []
        settings = {'rewrite': False, 'unsupported': False, 'text': 'I am interested.'}

        def transport(payload, before_send):
            before_send()
            sent.append(payload)
            data = json.loads(payload['input'])
            name = payload['text']['format']['name']
            targets = data['context']['targets']
            if name == 'PlanOutput':
                output = {'targets': [{'target': target['question']['id'], 'outline': [],
                    'evidence_ids': [], 'gaps': []} for target in targets]}
            elif name == 'DraftOutput':
                output = {'targets': [{'target': target['question']['id'], 'text': settings['text']}
                                      for target in targets]}
            else:
                assessments = []
                for item in data['draft']['targets']:
                    span = {'start': 0, 'end': len(item['text']), 'excerpt': item['text']}
                    claims = ([{**span, 'kind': 'personal', 'assessment': 'unsupported',
                                'evidence_ids': []}] if settings['unsupported'] else [])
                    assessments.append({'target': item['target'], 'inventory_complete': True,
                        'claims': claims, 'nonfactual_ranges': [] if claims else [span]})
                output = {'text_sha256': data['text_sha256'], 'targets': assessments,
                          'style_notes': [], 'rewrite_required': settings['rewrite']}
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(output)}]}], 'usage': {'total_tokens': 20}}

        provider = Provider(bundle.jobs, lambda: 'synthetic-literal-NOT-A-REAL-KEY', transport)
        provider.configure_limits(bundle.owner, Limits(10_000_000, 100, 2_000_000, 5))
        service = DraftingService(bundle.store, jobs=bundle.jobs, provider=provider)
        with bundle.store._read() as database:
            revisions = bundle.jobs.capture(database, bundle.owner, bundle.application_id)
        preview_request = DraftPreviewRequest(batch_id=batch.id, batch_sha256=batch.batch_sha256,
            expected_revisions=revisions, model='synthetic-model')
        preview = service.preview(bundle.owner, bundle.application_id, preview_request)
        request = DraftRequest(**preview_request.model_dump(), idempotency_key='synthetic-draft',
            disclosure_sha256=preview['disclosure_sha256'], acknowledged=True)
        admission = service.admission_state(bundle.owner, bundle.application_id)
        launch = DraftLaunch(admission_revision=admission['admission_revision'], request=request)
        job = service.enqueue(bundle.owner, bundle.application_id, launch)
        worker = str(uuid4())
        lease = bundle.jobs.claim(worker, job_id=job.id)
        def execute():
            # Reinstall the same hard tripwires when a deferred fixture run occurs.
            with ExitStack() as guards:
                guards.enter_context(patch('builtins.__import__', checked_import))
                guards.enter_context(patch.object(type(os.environ), 'get', checked_get))
                guards.enter_context(patch('socket.socket', forbidden))
                guards.enter_context(patch('copilot.provider.ResponsesHTTP.__call__', forbidden))
                guards.enter_context(patch('copilot.provider.ProviderHTTPS', forbidden))
                return service.run_draft(job.id, worker, lease.fence)

        draft = execute() if run else None
        assert len(sent) == (3 if run else 0)
        return DraftingHistory(service, scope, batch, draft, job, request, sent, settings, lease, execute)
