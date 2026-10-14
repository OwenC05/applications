"""Structural archive checks are not logical restore validation or authority."""
import hashlib
import io
import json
import tarfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from copilot.contracts import EvidenceError
from copilot.restore import ArchiveLimits, _scan_archive

BACKUP_ID = '11111111-1111-4111-8111-111111111111'
OWNER = '22222222-2222-4222-8222-222222222222'
SOURCE = '33333333-3333-4333-8333-333333333333'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fixture(tmp_path, *, edit=None, payloads=None):
    payloads = payloads or [('evidence.sqlite3', b'not yet a validated SQLite database'),
                            ('blobs/' + SOURCE, b'original synthetic bytes')]
    entries = [{'path': name, 'type': 'file', 'size': len(data), 'sha256': digest(data)}
               for name, data in payloads]
    manifest = {'format': 'copilot-backup', 'version': 1, 'backup_id': BACKUP_ID,
                'source_schema_sha256': 'a' * 64,
                'application_compatibility': 'application-copilot-evidence-0.2.0',
                'model_lock_sha256': 'b' * 64, 'installation_id': OWNER, 'entries': entries,
                'originals': [{'source_id': SOURCE, 'owner': OWNER, 'source_kind': 'source',
                               'path': 'blobs/' + SOURCE, 'sha256': entries[1]['sha256']}],
                'snapshot': {'method': 'offline-stage-backup-v1',
                             'raw_capture_sha256': 'c' * 64,
                             'canonical_database_sha256': entries[0]['sha256']}}
    if edit:
        edit(manifest)
    target = tmp_path / 'backup.tar'
    with tarfile.open(target, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in [('manifest.json', json.dumps(manifest).encode()), *payloads]:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return target


def scan(path, **kwargs):
    return _scan_archive(path, digest(path.read_bytes()), **kwargs)


def test_structural_result_is_frozen_not_logical_authority(tmp_path):
    path = fixture(tmp_path)
    before = path.read_bytes()
    result = scan(path)
    assert result.backup_id == BACKUP_ID
    assert result.entries[0].path == 'evidence.sqlite3'
    assert isinstance(result.entries, tuple)
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        result.backup_id = OWNER
    import copilot.restore as restore
    assert not hasattr(restore, 'ValidatedCapture')
    assert not hasattr(restore, 'inspect_archive')
    assert not hasattr(restore, 'prepare_destination')


@pytest.mark.parametrize('edit', [
    lambda m: m.update(version=True),
    lambda m: m.update(unexpected='secret'),
    lambda m: m['entries'][0].update(size=True),
    lambda m: m['entries'][0].update(path='../evidence.sqlite3'),
    lambda m: m['originals'][0].update(owner='not-a-uuid'),
    lambda m: m['originals'][0].update(sha256='e' * 64),
    lambda m: m['snapshot'].update(canonical_database_sha256='f' * 64),
    lambda m: m.update(installation_id=OWNER.upper().replace('2', 'A', 1)),
])
def test_closed_manifest_denials(tmp_path, edit):
    with pytest.raises(EvidenceError) as error:
        scan(fixture(tmp_path, edit=edit))
    assert error.value.code == 'RESTORE_ARCHIVE_INVALID'
    assert 'secret' not in str(error.value)


def test_archive_selection_hash_and_symlinks(tmp_path):
    path = fixture(tmp_path)
    with pytest.raises(EvidenceError) as error:
        _scan_archive(path, '0' * 64)
    assert error.value.code == 'RESTORE_ARCHIVE_HASH'
    link = tmp_path / 'link.tar'
    link.symlink_to(path)
    with pytest.raises(EvidenceError):
        _scan_archive(link, digest(path.read_bytes()))
    parent = tmp_path / 'linked-parent'
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(EvidenceError):
        _scan_archive(parent / path.name, digest(path.read_bytes()))


def test_no_caller_writes_or_sdk_imports(tmp_path, monkeypatch):
    import sys

    import copilot.restore as restore
    before = set(sys.modules)
    path = fixture(tmp_path)
    monkeypatch.setattr(Path, 'write_bytes', lambda *a: pytest.fail('unexpected write'))
    monkeypatch.setattr(Path, 'write_text', lambda *a: pytest.fail('unexpected write'))
    scan(path)
    assert not any(name.startswith(('chromadb', 'copilot.store', 'sqlite3'))
                   for name in set(sys.modules) - before)
    assert restore.__doc__


@pytest.mark.parametrize('limits', [ArchiveLimits(entries=1), ArchiveLimits(manifest_bytes=1),
                                    ArchiveLimits(per_payload_bytes=1),
                                    ArchiveLimits(aggregate_payload_bytes=1)])
def test_lower_bounds_are_enforced(tmp_path, limits):
    with pytest.raises(EvidenceError) as error:
        scan(fixture(tmp_path), limits=limits)
    assert error.value.code == 'RESTORE_ARCHIVE_LIMIT'


@pytest.mark.parametrize('field,value', [('entries', True), ('manifest_bytes', 0),
                                        ('entries', 10001), ('path_ascii_bytes', 241)])
def test_limits_cannot_raise_hard_caps(field, value):
    with pytest.raises(EvidenceError):
        ArchiveLimits(**{field: value})


def test_fabricated_limits_are_revalidated_at_scan(tmp_path):
    limits = ArchiveLimits()
    object.__setattr__(limits, 'entries', 10001)
    with pytest.raises(EvidenceError) as error:
        scan(fixture(tmp_path), limits=limits)
    assert error.value.code == 'RESTORE_ARCHIVE_LIMIT'


def test_lower_caps_allow_maximal_standard_record_alignment(tmp_path):
    # Manifest padding and a near-record-sized payload require more than 10240
    # bytes beyond the loose payload/header/padding upper bound.
    data = b'x' * 6657
    manifest = {'format': 'copilot-backup', 'version': 1, 'backup_id': BACKUP_ID,
                'source_schema_sha256': 'a' * 64,
                'application_compatibility': 'application-copilot-evidence-0.2.0',
                'model_lock_sha256': 'b' * 64, 'installation_id': OWNER,
                'entries': [{'path': 'evidence.sqlite3', 'type': 'file',
                             'size': len(data), 'sha256': digest(data)}], 'originals': [],
                'snapshot': {'method': 'offline-stage-backup-v1',
                             'raw_capture_sha256': 'c' * 64,
                             'canonical_database_sha256': digest(data)}}
    encoded = json.dumps(manifest).encode().ljust(1025, b' ')
    assert len(encoded) == 1025
    path = tmp_path / 'aligned.tar'
    with tarfile.open(path, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name, content in [('manifest.json', encoded), ('evidence.sqlite3', data)]:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    assert path.stat().st_size == 20480
    result = scan(path, limits=ArchiveLimits(entries=1, manifest_bytes=1025,
                  aggregate_payload_bytes=6657, per_payload_bytes=6657))
    assert result.entries[0].size == 6657


def test_nul_selected_path_has_safe_fixed_error():
    with pytest.raises(EvidenceError) as error:
        _scan_archive(Path('/tmp/private-selection\0.tar'), 'a' * 64)
    assert error.value.code == 'RESTORE_ARCHIVE_INVALID'
    assert 'private-selection' not in str(error.value)


def test_selection_io_error_does_not_chain_private_path(tmp_path):
    import traceback
    selected = tmp_path / 'private-missing-archive-canary.tar'
    try:
        _scan_archive(selected, 'a' * 64)
    except EvidenceError as error:
        assert error.code == 'RESTORE_ARCHIVE_IO'
        assert selected.name not in traceback.format_exc()
    else:
        pytest.fail('missing archive accepted')
