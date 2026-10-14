"""Hostile synthetic archives must never become restore or extraction authority."""
import dataclasses
import gzip
import json
import os

import pytest
from restore_archive_fixtures import (
    BLOB,
    DB,
    SOURCE,
    archive,
    digest,
    manifest,
    member,
)

from copilot import restore
from copilot.contracts import EvidenceError


def scan(tmp_path, raw, **kwargs):
    selected = tmp_path / 'selected.tar'
    selected.write_bytes(raw)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    try:
        return restore._scan_archive(selected, digest(raw), **kwargs)
    finally:
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
        assert not any(p.is_dir() for p in tmp_path.iterdir())


def denied(tmp_path, raw, **kwargs):
    with pytest.raises(EvidenceError) as failure:
        scan(tmp_path, raw, **kwargs)
    assert getattr(failure.value, 'code', None) in {
        'RESTORE_ARCHIVE_INVALID', 'RESTORE_ARCHIVE_LIMIT', 'RESTORE_ARCHIVE_CHANGED',
        'RESTORE_ARCHIVE_HASH', 'RESTORE_ARCHIVE_IO',
    }


def test_opaque_database_acceptance_is_explicitly_not_logical_restore_authority(tmp_path):
    result = scan(tmp_path, archive())
    assert result.archive_sha256 == digest(archive())
    assert [entry.path for entry in result.entries] == ['evidence.sqlite3', f'blobs/{SOURCE}']
    fabricated = dataclasses.replace(result, archive_sha256='0' * 64)
    assert fabricated.archive_sha256 == '0' * 64
    assert not any(hasattr(restore, name) for name in (
        'ValidatedCapture', 'inspect_archive', 'prepare_destination', 'restore', 'backup'))


@pytest.mark.parametrize('key,value', [
    ('version', True), ('version', 1.0), ('version', '1'), ('version', None),
    ('version', 2), ('format', 'copilot-backup\x00'), ('backup_id', SOURCE.upper()),
    ('source_schema_sha256', 'A' * 64), ('model_lock_sha256', 'z' * 64),
    ('entries', {}), ('originals', None), ('snapshot', []),
    ('application_compatibility', 'application-copilot-evidence-0.2.0\u200b'),
])
def test_manifest_closed_scalars(tmp_path, key, value):
    data = manifest()
    # SOURCE's current digits-only UUID has no case distinction; substitute hex letters.
    if key == 'backup_id':
        value = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
    data[key] = value
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('raw', [
    b'{', b'[]', b'null', b'true', b'"text"', b'\xff',
    b'{"version":1,"version":1}',
    b'{"version":NaN}', b'{"version":Infinity}', b'{"version":-Infinity}',
])
def test_invalid_json_never_admitted(tmp_path, raw):
    denied(tmp_path, archive(manifest_bytes=raw))


@pytest.mark.parametrize('section', ['root', 'entry', 'original', 'snapshot'])
def test_unknown_keys_rejected_at_every_closed_layer(tmp_path, section):
    data = manifest()
    target = {'root': data, 'entry': data['entries'][0], 'original': data['originals'][0],
              'snapshot': data['snapshot']}[section]
    target['unexpected'] = 'must not be ignored'
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('path', [
    '../escape', '/absolute', 'C:/escape', '\\\\server\\share', 'blobs/../escape',
    'blobs//x', './evidence.sqlite3', 'blobs/./x', 'blobs\\x', 'blobs/x\x00hidden',
    'blobs/\u2024\u2024/x', 'blobs/\u0430', 'x' * 241, '',
])
def test_manifest_paths_reject_traversal_aliases_confusables(tmp_path, path):
    data = manifest()
    data['entries'][1]['path'] = path
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('key,value', [
    ('size', True), ('size', -1), ('size', 1.0), ('size', '1'), ('size', float('nan')),
    ('size', 2**80), ('type', 'symlink'), ('sha256', 'F' * 64), ('sha256', 'x'),
])
def test_entry_scalar_boundaries(tmp_path, key, value):
    data = manifest()
    data['entries'][0][key] = value
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'extra', 'reverse', 'hash', 'size',
                                       'orphan-original', 'missing-original', 'original-hash'])
def test_inventory_and_original_declarations_must_match_exactly(tmp_path, mutation):
    data = manifest()
    payloads = [('evidence.sqlite3', DB), (f'blobs/{SOURCE}', BLOB)]
    if mutation == 'duplicate':
        data['entries'].append(dict(data['entries'][1]))
    elif mutation == 'missing':
        payloads.pop()
    elif mutation == 'extra':
        payloads.append(('extra', b'x'))
    elif mutation == 'reverse':
        payloads.reverse()
    elif mutation == 'hash':
        payloads[1] = (f'blobs/{SOURCE}', BLOB + b'!')
    elif mutation == 'size':
        data['entries'][1]['size'] += 1
    elif mutation == 'orphan-original':
        data['originals'].append(dict(data['originals'][0]))
    elif mutation == 'missing-original':
        data['originals'] = []
    elif mutation == 'original-hash':
        data['originals'][0]['sha256'] = '0' * 64
    denied(tmp_path, archive(data, payloads=payloads))


@pytest.mark.parametrize('flag', [b'1', b'2', b'3', b'4', b'5', b'6', b'7', b'S', b'x', b'g',
                                 b'L', b'K', b'\0'])
def test_nonregular_and_extension_headers_rejected(tmp_path, flag):
    encoded = json.dumps(manifest()).encode()
    raw = member('manifest.json', encoded, typeflag=flag) + b'\0' * 1024
    denied(tmp_path, raw)


@pytest.mark.parametrize('offset,value', [
    (157, b'link'), (345, b'prefix'), (265, b'root'), (297, b'root'), (329, b'0'),
    (337, b'0'), (108, b'0000001\0'), (116, b'0000001\0'), (136, b'00000000001\0'),
    (100, b'0000644\0'), (257, b'ustar '), (263, b'01'), (500, b'x'),
    (124, b'\x80' + b'\0' * 11), (124, b'00000000009\0'),
])
def test_noncanonical_header_fields_rejected_even_with_valid_checksum(tmp_path, offset, value):
    encoded = json.dumps(manifest()).encode()
    raw = member('manifest.json', encoded, changes=[(offset, value)]) + b'\0' * 1024
    denied(tmp_path, raw)


@pytest.mark.parametrize('kind', ['checksum', 'padding', 'truncated-header', 'truncated-data',
                                  'one-end-block', 'post-end-data', 'extra-zero-record',
                                  'arbitrary-zero-tail', 'compressed', 'duplicate-member'])
def test_raw_tar_framing_has_no_hidden_members_or_ignored_tail(tmp_path, kind):
    raw = archive()
    if kind == 'checksum':
        raw = b'X' + raw[1:]
    elif kind == 'padding':
        encoded = json.dumps(manifest(), separators=(',', ':')).encode()
        at = 512 + len(encoded)
        raw = raw[:at] + b'X' + raw[at + 1:]
    elif kind == 'truncated-header':
        raw = raw[:511]
    elif kind == 'truncated-data':
        raw = raw[:513]
    elif kind == 'one-end-block':
        raw = raw[:-512]
    elif kind == 'post-end-data':
        raw += member('hidden', b'secret')
    elif kind == 'extra-zero-record':
        raw += b'\0' * (10240 + (-len(raw) % 10240))
    elif kind == 'arbitrary-zero-tail':
        raw += b'\0' * 512
    elif kind == 'compressed':
        raw = gzip.compress(raw, mtime=0)
    elif kind == 'duplicate-member':
        raw = raw[:-1024] + member(f'blobs/{SOURCE}', BLOB) + b'\0' * 1024
    denied(tmp_path, raw)


@pytest.mark.parametrize('field,value', [('manifest_bytes', 10), ('entries', 1),
                                       ('aggregate_payload_bytes', len(DB)),
                                       ('per_payload_bytes', 1), ('path_ascii_bytes', 10)])
def test_stricter_caps_deny_instead_of_truncating(tmp_path, field, value):
    denied(tmp_path, archive(), limits=restore.ArchiveLimits(**{field: value}))


@pytest.mark.parametrize('field,maximum', [('manifest_bytes', 8 * 1024**2), ('entries', 10000),
    ('aggregate_payload_bytes', 2 * 1024**3), ('per_payload_bytes', 256 * 1024**2),
    ('path_ascii_bytes', 240)])
@pytest.mark.parametrize('kind', ['bool', 'zero', 'negative', 'float', 'raise'])
def test_limits_cannot_raise_hard_caps_or_use_bool_as_int(field, maximum, kind):
    value = {'bool': True, 'zero': 0, 'negative': -1, 'float': 1.0, 'raise': maximum + 1}[kind]
    with pytest.raises((ValueError, TypeError, EvidenceError)):
        restore.ArchiveLimits(**{field: value})


def test_default_caps_are_contract_values():
    bounds = restore.ArchiveLimits()
    assert (bounds.manifest_bytes, bounds.entries, bounds.aggregate_payload_bytes,
            bounds.per_payload_bytes, bounds.path_ascii_bytes) == (
                8388608, 10000, 2147483648, 268435456, 240)


def test_wrong_selected_archive_digest_denies_without_writes(tmp_path):
    selected = tmp_path / 'selected.tar'
    raw = archive()
    selected.write_bytes(raw)
    with pytest.raises(EvidenceError) as failure:
        restore._scan_archive(selected, '0' * 64)
    assert failure.value.code == 'RESTORE_ARCHIVE_HASH'
    assert selected.read_bytes() == raw


@pytest.mark.parametrize('kind', ['missing', 'directory', 'symlink', 'parent-symlink', 'hardlink', 'fifo'])
def test_selected_archive_must_be_single_regular_nofollow_file(tmp_path, kind):
    real = tmp_path / 'real.tar'
    real.write_bytes(archive())
    selected = tmp_path / 'selected.tar'
    if kind == 'directory':
        selected.mkdir()
    elif kind == 'symlink':
        selected.symlink_to(real)
    elif kind == 'parent-symlink':
        folder = tmp_path / 'folder'
        folder.mkdir()
        (folder / 'archive.tar').write_bytes(archive())
        alias = tmp_path / 'alias'
        alias.symlink_to(folder, target_is_directory=True)
        selected = alias / 'archive.tar'
    elif kind == 'hardlink':
        os.link(real, selected)
    elif kind == 'fifo':
        os.mkfifo(selected)
    with pytest.raises(EvidenceError) as failure:
        restore._scan_archive(selected, digest(archive()))
    assert failure.value.code in {'RESTORE_ARCHIVE_IO', 'RESTORE_ARCHIVE_INVALID', 'RESTORE_ARCHIVE_CHANGED'}
    assert real.read_bytes() == archive()


@pytest.mark.parametrize('needle,replacement', [
    ('"version": 1', '"version": 1, "version": 1'),
    ('"type": "file"', '"type": "file", "type": "file"'),
    ('"method": "offline-stage-backup-v1"',
     '"method": "offline-stage-backup-v1", "method": "offline-stage-backup-v1"'),
    ('"owner": "22222222-2222-4222-8222-222222222222"',
     '"owner": "22222222-2222-4222-8222-222222222222", "owner": "22222222-2222-4222-8222-222222222222"'),
])
def test_duplicate_keys_reject_otherwise_valid_manifest(tmp_path, needle, replacement):
    encoded = json.dumps(manifest())
    assert needle in encoded
    denied(tmp_path, archive(manifest_bytes=encoded.replace(needle, replacement, 1).encode()))


@pytest.mark.parametrize('value', [float('inf'), float('-inf')])
def test_nonfinite_nested_size_rejected(tmp_path, value):
    data = manifest()
    data['entries'][1]['size'] = value
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('name', [b'/absolute', b'../escape', b'C:/escape', b'bad\\path',
                                  b'manifest.json\0hidden', b'\xffname'])
def test_header_names_cannot_hide_unsafe_wire_paths(tmp_path, name):
    denied(tmp_path, member(name, json.dumps(manifest()).encode()) + b'\0' * 1024)


def test_normal_tar_record_alignment_is_accepted(tmp_path):
    raw = archive()
    padded = raw + b'\0' * (-len(raw) % 10240)
    assert scan(tmp_path, padded).archive_sha256 == digest(padded)


def test_declared_giant_manifest_is_rejected_before_body_allocation(tmp_path):
    from restore_archive_fixtures import header

    denied(tmp_path, header('manifest.json', 8 * 1024**2 + 1) + b'\0' * 1024)


@pytest.mark.parametrize('key,value', [('source_id', '../escape'), ('owner', True),
                                       ('source_kind', 'workspace:other'), ('path', 'evidence.sqlite3'),
                                       ('sha256', '0' * 64)])
def test_original_relationship_scalars_rejected(tmp_path, key, value):
    data = manifest()
    data['originals'][0][key] = value
    denied(tmp_path, archive(data))


def test_snapshot_database_digest_must_match_inventory(tmp_path):
    data = manifest()
    data['snapshot']['canonical_database_sha256'] = '0' * 64
    denied(tmp_path, archive(data))


@pytest.mark.parametrize('mutation', ['replace-identical', 'replace-different', 'modify-open-file',
                                      'replace-with-symlink', 'replace-parent'])
def test_selected_archive_replacement_is_detected_after_open(tmp_path, monkeypatch, mutation):
    folder = tmp_path / 'selected-parent'
    folder.mkdir()
    selected = folder / 'selected.tar'
    raw = archive()
    selected.write_bytes(raw)
    replacement = tmp_path / 'replacement.tar'
    replacement.write_bytes(raw if mutation != 'replace-different' else raw + b'changed')
    original_read = os.read
    fired = False

    def intercept(fd, size):
        nonlocal fired
        data = original_read(fd, size)
        if not fired:
            fired = True
            if mutation.startswith('replace-') and mutation not in {
                'replace-with-symlink', 'replace-parent',
            }:
                os.replace(replacement, selected)
            elif mutation == 'modify-open-file':
                with selected.open('r+b') as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b'X')
            elif mutation == 'replace-with-symlink':
                selected.unlink()
                selected.symlink_to(replacement)
            elif mutation == 'replace-parent':
                folder.rename(tmp_path / 'old-parent')
                folder.mkdir()
                selected.write_bytes(raw)
        return data

    monkeypatch.setattr(os, 'read', intercept)
    with pytest.raises(EvidenceError) as failure:
        restore._scan_archive(selected, digest(raw))
    assert fired
    assert failure.value.code in {'RESTORE_ARCHIVE_CHANGED', 'RESTORE_ARCHIVE_INVALID',
                                   'RESTORE_ARCHIVE_HASH', 'RESTORE_ARCHIVE_IO'}
    # Only this test's deterministic adversary writes; helper never extracts originals.
    assert not (folder / 'blobs').exists()


def test_payload_hashing_uses_bounded_read_chunks(tmp_path, monkeypatch):
    data = manifest()
    large = b'synthetic payload\n' * 18000
    data['entries'][1]['size'] = len(large)
    data['entries'][1]['sha256'] = digest(large)
    data['originals'][0]['sha256'] = digest(large)
    raw = archive(data, payloads=[('evidence.sqlite3', DB), (f'blobs/{SOURCE}', large)])
    requested = []
    original_read = os.read

    def observed(fd, size):
        requested.append(size)
        assert 0 < size <= 65536
        return original_read(fd, size)

    monkeypatch.setattr(os, 'read', observed)
    result = scan(tmp_path, raw)
    assert result.entries[1].sha256 == digest(large)
    assert len(requested) > 5


def test_total_archive_size_cap_rejects_before_any_payload_read(tmp_path, monkeypatch):
    selected = tmp_path / 'selected.tar'
    raw = archive()
    selected.write_bytes(raw + b'\0' * 20000)
    observed = []
    original_read = os.read

    def intercepted(fd, size):
        observed.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, 'read', intercepted)
    limits = restore.ArchiveLimits(manifest_bytes=1, entries=1, aggregate_payload_bytes=1,
                                  per_payload_bytes=1, path_ascii_bytes=1)
    with pytest.raises(EvidenceError) as failure:
        restore._scan_archive(selected, digest(selected.read_bytes()), limits=limits)
    assert failure.value.code == 'RESTORE_ARCHIVE_LIMIT'
    assert observed == []


def test_unmodified_python_ustar_writer_independently_confirms_positive_wire_format(tmp_path):
    import io
    import tarfile

    output = io.BytesIO()
    encoded = json.dumps(manifest()).encode('utf-8')
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT,
                      encoding='ascii', errors='strict') as writer:
        for name, payload in [('manifest.json', encoded), ('evidence.sqlite3', DB),
                              (f'blobs/{SOURCE}', BLOB)]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = info.linkname = ''
            info.type = tarfile.REGTYPE
            writer.addfile(info, io.BytesIO(payload))
    raw = output.getvalue()
    assert len(raw) % tarfile.RECORDSIZE == 0
    assert raw[329:345] == b'\0' * 16  # stdlib leaves regular-file device fields empty.
    result = scan(tmp_path, raw)
    assert result.archive_sha256 == digest(raw)
    assert result.entries[1].sha256 == digest(BLOB)
