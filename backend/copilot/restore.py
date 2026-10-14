"""Inactive structural/catalog inspection, NOT logical validation or restore authority.

Inventories and declared provenance are caller-fabricable data. This module does
not validate application relationships, extract originals, install a capture,
or authorize destination preparation. The separate private catalog observer
opens only a copied owned database in a resource-bounded disposable child. Hashes prove byte selection, not
origin authentication. No runtime/CLI path currently invokes this scanner.
"""
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from .contracts import EvidenceError

_BLOCK = 512
_CHUNK = 65536
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_COMPATIBILITY = 'application-copilot-evidence-0.2.0'
_MESSAGES = {
    'INVALID': 'Unsupported or malformed backup archive',
    'LIMIT': 'Backup archive exceeds supported bounds',
    'CHANGED': 'Selected backup archive changed during inspection',
    'HASH': 'Selected backup archive digest does not match',
    'IO': 'Selected backup archive could not be read safely',
}


def _deny(kind):
    raise EvidenceError('RESTORE_ARCHIVE_' + kind, _MESSAGES[kind]) from None


@dataclass(frozen=True)
class ArchiveLimits:
    manifest_bytes: int = 8388608
    entries: int = 10000
    aggregate_payload_bytes: int = 2147483648
    per_payload_bytes: int = 268435456
    path_ascii_bytes: int = 240

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= field.default:
                _deny('LIMIT')


@dataclass(frozen=True)
class StructuralEntry:
    path: str
    type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class StructuralInventory:
    """Nonauthoritative declarations; never a destination-install capability."""
    archive_sha256: str
    backup_id: str
    entries: tuple[StructuralEntry, ...]
    source_schema_sha256: str
    application_compatibility: str
    model_lock_sha256: str
    installation_id: str


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected.split()):
        _deny('INVALID')


def _uuid(value):
    if type(value) is not str:
        _deny('INVALID')
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        _deny('INVALID')
    if canonical != value:
        _deny('INVALID')
    return value


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        _deny('INVALID')
    return value


def _path(value, limits):
    if type(value) is not str:
        _deny('INVALID')
    try:
        encoded = value.encode('ascii')
    except UnicodeEncodeError:
        _deny('INVALID')
    if len(encoded) > limits.path_ascii_bytes:
        _deny('LIMIT')
    if value == 'evidence.sqlite3':
        return value
    if value.startswith('blobs/') and '/' not in value[6:]:
        _uuid(value[6:])
        return value
    _deny('INVALID')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _deny('INVALID')
        value[key] = item
    return value


def _manifest(data, limits):
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: _deny('INVALID'))
    except (UnicodeError, ValueError, RecursionError):
        _deny('INVALID')
    _keys(value, 'format version backup_id source_schema_sha256 application_compatibility '
                 'model_lock_sha256 installation_id entries originals snapshot')
    if (value['format'] != 'copilot-backup' or type(value['version']) is not int
            or value['version'] != 1 or value['application_compatibility'] != _COMPATIBILITY):
        _deny('INVALID')
    for name in ('backup_id', 'installation_id'):
        _uuid(value[name])
    for name in ('source_schema_sha256', 'model_lock_sha256'):
        _digest(value[name])
    if type(value['entries']) is not list or not value['entries']:
        _deny('INVALID')
    if len(value['entries']) > limits.entries:
        _deny('LIMIT')
    entries = []
    total = 0
    for item in value['entries']:
        _keys(item, 'path type size sha256')
        path = _path(item['path'], limits)
        size = item['size']
        if item['type'] != 'file' or type(size) is not int or size < 0:
            _deny('INVALID')
        total += size
        if size > limits.per_payload_bytes or total > limits.aggregate_payload_bytes:
            _deny('LIMIT')
        entries.append(StructuralEntry(path, 'file', size, _digest(item['sha256'])))
    names = [entry.path for entry in entries]
    if (names[0] != 'evidence.sqlite3' or len(set(names)) != len(names)
            or names[1:] != sorted(names[1:]) or any(not p.startswith('blobs/') for p in names[1:])):
        _deny('INVALID')
    originals = value['originals']
    if type(originals) is not list or len(originals) != len(entries) - 1:
        _deny('INVALID')
    for item, entry in zip(originals, entries[1:]):
        _keys(item, 'source_id owner source_kind path sha256')
        source_id = _uuid(item['source_id'])
        _uuid(item['owner'])
        if (item['source_kind'] not in ('source', 'workspace:employer_source')
                or item['path'] != 'blobs/' + source_id or item['path'] != entry.path
                or _digest(item['sha256']) != entry.sha256):
            _deny('INVALID')
    snapshot = value['snapshot']
    _keys(snapshot, 'method raw_capture_sha256 canonical_database_sha256')
    if (snapshot['method'] != 'offline-stage-backup-v1'
            or _digest(snapshot['canonical_database_sha256']) != entries[0].sha256):
        _deny('INVALID')
    _digest(snapshot['raw_capture_sha256'])
    return value, tuple(entries)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _selected_archive(path):
    # Traverse each directory by descriptor; never resolve through a symlink.
    try:
        path = Path(path)
        if '..' in path.parts or '\0' in str(path):
            _deny('INVALID')
        path = Path(os.path.abspath(path))
    except (TypeError, ValueError):
        _deny('INVALID')
    descriptors = []
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | os.O_CLOEXEC, dir_fd=directory)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _deny('INVALID')
        yield descriptor, before, path
        if _identity(os.fstat(descriptor)) != _identity(before):
            _deny('CHANGED')
        # Re-traverse from root to detect directory/name swaps as well as FD edits.
        with _reselected_identity(path) as selected:
            if _identity(selected) != _identity(before):
                _deny('CHANGED')
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _reselected_identity(path):
    # Separate final lookup without recursively repeating the final comparison.
    descriptors = []
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=directory)
            descriptors.append(directory)
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _deny('CHANGED')
        yield info
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _Reader:
    def __init__(self, descriptor, size):
        self.descriptor = descriptor
        self.size = size
        self.position = 0
        self.digest = hashlib.sha256()

    def read(self, amount):
        if amount < 0 or amount > _CHUNK or amount > self.size - self.position:
            _deny('INVALID')
        value = bytearray()
        while len(value) < amount:
            try:
                part = os.read(self.descriptor, amount - len(value))
            except OSError:
                _deny('IO')
            if not part:
                _deny('INVALID')
            value.extend(part)
        self.position += amount
        self.digest.update(value)
        return bytes(value)

    def payload(self, size, *, buffer=False, sink=None):
        digest = hashlib.sha256()
        output = bytearray() if buffer else None
        remaining = size
        while remaining:
            part = self.read(min(remaining, _CHUNK))
            remaining -= len(part)
            digest.update(part)
            if sink is not None:
                sink(part)
            if output is not None:
                output.extend(part)
        padding = (-size) % _BLOCK
        if padding and any(self.read(padding)):
            _deny('INVALID')
        return bytes(output) if output is not None else digest.hexdigest()


def _octal(value):
    if not value or value[-1:] != b'\0' or any(c not in b'01234567' for c in value[:-1]):
        _deny('INVALID')
    return int(value[:-1], 8)


def _header(reader):
    value = reader.read(_BLOCK)
    if not any(value):
        _deny('INVALID')
    name_field = value[:100]
    name, separator, tail = name_field.partition(b'\0')
    if not name or not separator or any(tail):
        _deny('INVALID')
    try:
        name = name.decode('ascii')
    except UnicodeError:
        _deny('INVALID')
    checksum = value[148:156]
    if (checksum[-2:] != b'\0 ' or any(c not in b'01234567' for c in checksum[:6])
            or int(checksum[:6], 8) != sum(value[:148]) + 8 * 32 + sum(value[156:])):
        _deny('INVALID')
    if (value[257:263] != b'ustar\0' or value[263:265] != b'00'
            or value[156:157] != b'0' or any(value[157:257]) or any(value[265:512])
            or _octal(value[100:108]) != 0o600 or _octal(value[108:116]) != 0
            or _octal(value[116:124]) != 0 or _octal(value[136:148]) != 0):
        _deny('INVALID')
    return name, _octal(value[124:136])


def _scan_selected(descriptor, before, expected_sha256, limits, *, database_sink=None):
    upper_payload_end = (limits.manifest_bytes + limits.aggregate_payload_bytes
                         + (limits.entries + 1) * (2 * _BLOCK - 1))
    max_size = ((upper_payload_end + 2 * _BLOCK + 10239) // 10240) * 10240
    if before.st_size > max_size:
        _deny('LIMIT')
    reader = _Reader(descriptor, before.st_size)
    name, size = _header(reader)
    if name != 'manifest.json':
        _deny('INVALID')
    if size > limits.manifest_bytes:
        _deny('LIMIT')
    manifest, entries = _manifest(reader.payload(size, buffer=True), limits)
    for index, entry in enumerate(entries):
        name, size = _header(reader)
        if name != entry.path or size != entry.size:
            _deny('INVALID')
        sink = database_sink if index == 0 else None
        if reader.payload(size, sink=sink) != entry.sha256:
            _deny('HASH')
    minimal_end = reader.position + 2 * _BLOCK
    record_end = ((minimal_end + 10239) // 10240) * 10240
    if before.st_size not in (minimal_end, record_end):
        _deny('INVALID')
    while reader.position < before.st_size:
        if any(reader.read(min(_CHUNK, before.st_size - reader.position))):
            _deny('INVALID')
    archive_digest = reader.digest.hexdigest()
    if archive_digest != expected_sha256:
        _deny('HASH')
    return StructuralInventory(archive_digest, manifest['backup_id'], entries,
                               manifest['source_schema_sha256'],
                               manifest['application_compatibility'],
                               manifest['model_lock_sha256'], manifest['installation_id'])


def _checked_limits(limits):
    if type(limits) is not ArchiveLimits:
        _deny('LIMIT')
    limits.__post_init__()


def _scan_archive(archive_path: Path, expected_sha256: str, *,
                  limits: ArchiveLimits = ArchiveLimits()) -> StructuralInventory:
    """Scan structural bytes only; never treat the returned data as trusted restore input."""
    _digest(expected_sha256)
    _checked_limits(limits)
    try:
        with _selected_archive(archive_path) as (descriptor, before, _):
            result = _scan_selected(descriptor, before, expected_sha256, limits)
        return result
    except OSError:
        _deny('IO')


# Pinned trusted Store+Jobs catalog; never regenerated from incoming data.
_SUPPORTED_CATALOG = json.loads('{"metadata":{"app_revisions":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_app_revisions_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_app_revisions_1":[[0,1,"application_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"owner","TEXT",1,null,0,0],[1,"application_id","TEXT",0,null,1,0],[2,"research","INTEGER",1,"0",0,0]]},"budget_limits":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_budget_limits_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_budget_limits_1":[[0,0,"owner",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"owner","TEXT",0,null,1,0],[1,"daily_tokens","INTEGER",1,null,0,0],[2,"daily_calls","INTEGER",1,null,0,0],[3,"job_tokens","INTEGER",1,null,0,0],[4,"job_calls","INTEGER",1,null,0,0]]},"dense_mutations":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_dense_mutations_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_dense_mutations_1":[[0,0,"generation_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"generation_id","TEXT",0,null,1,0],[1,"fence","INTEGER",1,null,0,0],[2,"state","TEXT",1,null,0,0]]},"drafting_admission_bindings":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_drafting_admission_bindings_2",1,"u",0],[1,"sqlite_autoindex_drafting_admission_bindings_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_drafting_admission_bindings_1":[[0,0,"job_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]],"sqlite_autoindex_drafting_admission_bindings_2":[[0,1,"owner",0,"BINARY",1],[1,2,"application_id",0,"BINARY",1],[2,5,"captured_revision",0,"BINARY",1],[3,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"job_id","TEXT",1,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"application_id","TEXT",1,null,0,0],[3,"kind","TEXT",1,null,0,0],[4,"origin","TEXT",1,null,0,0],[5,"captured_revision","INTEGER",0,null,0,0],[6,"consumed_revision","INTEGER",0,null,0,0],[7,"request_sha256","TEXT",0,null,0,0],[8,"envelope_sha256","TEXT",0,null,0,0]]},"drafting_admission_heads":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_drafting_admission_heads_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_drafting_admission_heads_1":[[0,0,"owner",0,"BINARY",1],[1,1,"application_id",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"owner","TEXT",1,null,1,0],[1,"application_id","TEXT",1,null,2,0],[2,"revision","INTEGER",1,null,0,0],[3,"last_job_id","TEXT",0,null,0,0]]},"fact_metadata":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_fact_metadata_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_fact_metadata_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"data","TEXT",1,null,0,0]]},"generation_chunks":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_generation_chunks_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_generation_chunks_1":[[0,0,"generation_id",0,"BINARY",1],[1,1,"id",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"generation_id","TEXT",1,null,1,0],[1,"id","TEXT",1,null,2,0],[2,"owner","TEXT",1,null,0,0],[3,"data","TEXT",1,null,0,0]]},"generation_intents":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_generation_intents_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_generation_intents_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"corpus","TEXT",1,null,0,0],[3,"state","TEXT",1,null,0,0],[4,"data","TEXT",1,null,0,0],[5,"captured","TEXT",0,null,0,0]]},"job_keys":{"foreign_key_list":[],"index_list":[[0,"job_keys_scope",0,"c",0],[1,"sqlite_autoindex_job_keys_1",1,"pk",0]],"index_xinfo":{"job_keys_scope":[[0,0,"owner",0,"BINARY",1],[1,4,"application_id",0,"BINARY",1],[2,-1,null,0,"BINARY",0]],"sqlite_autoindex_job_keys_1":[[0,0,"owner",0,"BINARY",1],[1,1,"key",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"owner","TEXT",1,null,1,0],[1,"key","TEXT",1,null,2,0],[2,"job_id","TEXT",1,null,0,0],[3,"request_sha256","TEXT",1,null,0,0],[4,"application_id","TEXT",0,null,0,0],[5,"kind","TEXT",0,null,0,0]]},"job_retries":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_job_retries_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_job_retries_1":[[0,0,"new_job_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"new_job_id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"prior_job_id","TEXT",1,null,0,0],[3,"prior_attempt_id","TEXT",1,null,0,0],[4,"warning_version","TEXT",1,null,0,0]]},"job_stages":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_job_stages_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_job_stages_1":[[0,0,"job_id",0,"BINARY",1],[1,1,"stage",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"job_id","TEXT",1,null,1,0],[1,"stage","TEXT",1,null,2,0],[2,"fence","INTEGER",1,null,0,0],[3,"data","TEXT",1,null,0,0],[4,"sha256","TEXT",1,null,0,0]]},"jobs":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_jobs_2",1,"u",0],[1,"sqlite_autoindex_jobs_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_jobs_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]],"sqlite_autoindex_jobs_2":[[0,1,"owner",0,"BINARY",1],[1,0,"id",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"state","TEXT",1,null,0,0],[3,"data","TEXT",1,null,0,0],[4,"parameters","TEXT",1,null,0,0],[5,"dependencies","TEXT",1,null,0,0]]},"manifests":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_manifests_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_manifests_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"corpus","TEXT",1,null,0,0],[3,"state","TEXT",1,null,0,0],[4,"data","TEXT",1,null,0,0]]},"profile_revisions":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_profile_revisions_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_profile_revisions_1":[[0,0,"owner",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"owner","TEXT",0,null,1,0],[1,"metadata","INTEGER",1,"0",0,0],[2,"facts","INTEGER",1,"0",0,0],[3,"documents","INTEGER",1,"0",0,0],[4,"consent","INTEGER",1,"0",0,0]]},"profiles":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_profiles_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_profiles_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"data","TEXT",1,null,0,0]]},"provider_attempts":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_provider_attempts_2",1,"u",0],[1,"sqlite_autoindex_provider_attempts_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_provider_attempts_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]],"sqlite_autoindex_provider_attempts_2":[[0,2,"job_id",0,"BINARY",1],[1,8,"stage",0,"BINARY",1],[2,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"job_id","TEXT",1,null,0,0],[3,"state","TEXT",1,null,0,0],[4,"data","TEXT",1,null,0,0],[5,"dispatched","INTEGER",1,"0",0,0],[6,"day","TEXT",1,null,0,0],[7,"response","TEXT",0,null,0,0],[8,"stage","TEXT",1,null,0,0],[9,"request_sha256","TEXT",1,null,0,0]]},"provider_retry_links":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_provider_retry_links_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_provider_retry_links_1":[[0,0,"attempt_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"attempt_id","TEXT",0,null,1,0],[1,"prior_attempt_id","TEXT",1,null,0,0]]},"records":{"foreign_key_list":[],"index_list":[[0,"record_owner",0,"c",0],[1,"sqlite_autoindex_records_1",1,"pk",0]],"index_xinfo":{"record_owner":[[0,1,"owner",0,"BINARY",1],[1,2,"kind",0,"BINARY",1],[2,-1,null,0,"BINARY",0]],"sqlite_autoindex_records_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"kind","TEXT",1,null,0,0],[3,"data","TEXT",1,null,0,0]]},"research_blob_origins":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_research_blob_origins_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_research_blob_origins_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"application_id","TEXT",1,null,0,0],[3,"run_id","TEXT",1,null,0,0],[4,"intent_id","TEXT",1,null,0,0],[5,"job_id","TEXT",1,null,0,0],[6,"fence","INTEGER",1,null,0,0]]},"research_blob_scopes":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_research_blob_scopes_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_research_blob_scopes_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"application_id","TEXT",1,null,0,0],[3,"run_id","TEXT",1,null,0,0],[4,"intent_id","TEXT",1,null,0,0]]},"research_heads":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_research_heads_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_research_heads_1":[[0,0,"application_id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"application_id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"run_id","TEXT",1,null,0,0],[3,"research","INTEGER",1,null,0,0],[4,"output","INTEGER",1,null,0,0]]},"research_intents":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_research_intents_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_research_intents_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"application_id","TEXT",1,null,0,0],[3,"job_id","TEXT",1,null,0,0],[4,"fence","INTEGER",1,null,0,0],[5,"run_id","TEXT",0,null,0,0],[6,"state","TEXT",1,null,0,0]]},"tickets":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_tickets_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_tickets_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"data","TEXT",1,null,0,0]]},"tombstones":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_tombstones_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_tombstones_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"kind","TEXT",1,null,0,0]]},"upload_intents":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_upload_intents_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_upload_intents_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"owner","TEXT",1,null,0,0],[2,"state","TEXT",1,null,0,0],[3,"blob_sha256","TEXT",1,null,0,0]]},"upload_receipts":{"foreign_key_list":[],"index_list":[[0,"sqlite_autoindex_upload_receipts_1",1,"pk",0]],"index_xinfo":{"sqlite_autoindex_upload_receipts_1":[[0,0,"id",0,"BINARY",1],[1,-1,null,0,"BINARY",0]]},"table_xinfo":[[0,"id","TEXT",0,null,1,0],[1,"device","INTEGER",1,null,0,0],[2,"inode","INTEGER",1,null,0,0]]}},"objects":[["index","job_keys_scope","job_keys","CREATE INDEX job_keys_scope ON job_keys(owner,application_id)"],["index","record_owner","records","CREATE INDEX record_owner ON records(owner,kind)"],["index","sqlite_autoindex_app_revisions_1","app_revisions",null],["index","sqlite_autoindex_budget_limits_1","budget_limits",null],["index","sqlite_autoindex_dense_mutations_1","dense_mutations",null],["index","sqlite_autoindex_drafting_admission_bindings_1","drafting_admission_bindings",null],["index","sqlite_autoindex_drafting_admission_bindings_2","drafting_admission_bindings",null],["index","sqlite_autoindex_drafting_admission_heads_1","drafting_admission_heads",null],["index","sqlite_autoindex_fact_metadata_1","fact_metadata",null],["index","sqlite_autoindex_generation_chunks_1","generation_chunks",null],["index","sqlite_autoindex_generation_intents_1","generation_intents",null],["index","sqlite_autoindex_job_keys_1","job_keys",null],["index","sqlite_autoindex_job_retries_1","job_retries",null],["index","sqlite_autoindex_job_stages_1","job_stages",null],["index","sqlite_autoindex_jobs_1","jobs",null],["index","sqlite_autoindex_jobs_2","jobs",null],["index","sqlite_autoindex_manifests_1","manifests",null],["index","sqlite_autoindex_profile_revisions_1","profile_revisions",null],["index","sqlite_autoindex_profiles_1","profiles",null],["index","sqlite_autoindex_provider_attempts_1","provider_attempts",null],["index","sqlite_autoindex_provider_attempts_2","provider_attempts",null],["index","sqlite_autoindex_provider_retry_links_1","provider_retry_links",null],["index","sqlite_autoindex_records_1","records",null],["index","sqlite_autoindex_research_blob_origins_1","research_blob_origins",null],["index","sqlite_autoindex_research_blob_scopes_1","research_blob_scopes",null],["index","sqlite_autoindex_research_heads_1","research_heads",null],["index","sqlite_autoindex_research_intents_1","research_intents",null],["index","sqlite_autoindex_tickets_1","tickets",null],["index","sqlite_autoindex_tombstones_1","tombstones",null],["index","sqlite_autoindex_upload_intents_1","upload_intents",null],["index","sqlite_autoindex_upload_receipts_1","upload_receipts",null],["table","app_revisions","app_revisions","CREATE TABLE app_revisions(owner TEXT NOT NULL,\\n                   application_id TEXT PRIMARY KEY, research INTEGER NOT NULL DEFAULT 0)"],["table","budget_limits","budget_limits","CREATE TABLE budget_limits(owner TEXT PRIMARY KEY,\\n                   daily_tokens INTEGER NOT NULL, daily_calls INTEGER NOT NULL,\\n                   job_tokens INTEGER NOT NULL, job_calls INTEGER NOT NULL)"],["table","dense_mutations","dense_mutations","CREATE TABLE dense_mutations(generation_id TEXT PRIMARY KEY,\\n                    fence INTEGER NOT NULL, state TEXT NOT NULL)"],["table","drafting_admission_bindings","drafting_admission_bindings","CREATE TABLE drafting_admission_bindings(\\n    job_id TEXT NOT NULL PRIMARY KEY, owner TEXT NOT NULL, application_id TEXT NOT NULL,\\n    kind TEXT NOT NULL CHECK(kind IN (\'draft\',\'assess\')),\\n    origin TEXT NOT NULL CHECK(origin IN (\'admitted\',\'legacy\')),\\n    captured_revision INTEGER, consumed_revision INTEGER,\\n    request_sha256 TEXT, envelope_sha256 TEXT,\\n    CHECK(typeof(job_id)=\'text\' AND typeof(owner)=\'text\' AND typeof(application_id)=\'text\'),\\n    CHECK(request_sha256 IS NULL OR (typeof(request_sha256)=\'text\' AND length(request_sha256)=64 AND request_sha256 NOT GLOB \'*[^0-9a-f]*\')),\\n    CHECK(envelope_sha256 IS NULL OR (typeof(envelope_sha256)=\'text\' AND length(envelope_sha256)=64 AND envelope_sha256 NOT GLOB \'*[^0-9a-f]*\')),\\n    CHECK((origin=\'legacy\' AND captured_revision IS NULL AND consumed_revision IS NULL AND envelope_sha256 IS NULL)\\n       OR (origin=\'admitted\' AND typeof(captured_revision)=\'integer\' AND captured_revision BETWEEN 0 AND 9007199254740990\\n           AND typeof(consumed_revision)=\'integer\' AND consumed_revision=captured_revision+1\\n           AND request_sha256 IS NOT NULL AND envelope_sha256 IS NOT NULL)),\\n    UNIQUE(owner,application_id,captured_revision))"],["table","drafting_admission_heads","drafting_admission_heads","CREATE TABLE drafting_admission_heads(\\n    owner TEXT NOT NULL, application_id TEXT NOT NULL,\\n    revision INTEGER NOT NULL CHECK(typeof(revision)=\'integer\' AND revision BETWEEN 0 AND 9007199254740991),\\n    last_job_id TEXT, PRIMARY KEY(owner,application_id),\\n    CHECK(typeof(owner)=\'text\' AND typeof(application_id)=\'text\'),\\n    CHECK(last_job_id IS NULL OR typeof(last_job_id)=\'text\'))"],["table","fact_metadata","fact_metadata","CREATE TABLE fact_metadata(id TEXT PRIMARY KEY, owner TEXT NOT NULL,\\n                    data TEXT NOT NULL)"],["table","generation_chunks","generation_chunks","CREATE TABLE generation_chunks(generation_id TEXT NOT NULL,\\n                    id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL,\\n                    PRIMARY KEY(generation_id,id))"],["table","generation_intents","generation_intents","CREATE TABLE generation_intents(id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, corpus TEXT NOT NULL, state TEXT NOT NULL,\\n                    data TEXT NOT NULL, captured TEXT)"],["table","job_keys","job_keys","CREATE TABLE job_keys(owner TEXT NOT NULL, key TEXT NOT NULL,\\n                   job_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, application_id TEXT, kind TEXT, PRIMARY KEY(owner,key))"],["table","job_retries","job_retries","CREATE TABLE job_retries(new_job_id TEXT PRIMARY KEY,\\n                   owner TEXT NOT NULL, prior_job_id TEXT NOT NULL, prior_attempt_id TEXT NOT NULL,\\n                   warning_version TEXT NOT NULL)"],["table","job_stages","job_stages","CREATE TABLE job_stages(job_id TEXT NOT NULL, stage TEXT NOT NULL,\\n                   fence INTEGER NOT NULL, data TEXT NOT NULL, sha256 TEXT NOT NULL,\\n                   PRIMARY KEY(job_id,stage))"],["table","jobs","jobs","CREATE TABLE jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL,\\n                   state TEXT NOT NULL, data TEXT NOT NULL, parameters TEXT NOT NULL,\\n                   dependencies TEXT NOT NULL, UNIQUE(owner,id))"],["table","manifests","manifests","CREATE TABLE manifests(id TEXT PRIMARY KEY, owner TEXT NOT NULL,\\n                    corpus TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)"],["table","profile_revisions","profile_revisions","CREATE TABLE profile_revisions(owner TEXT PRIMARY KEY,\\n                    metadata INTEGER NOT NULL DEFAULT 0, facts INTEGER NOT NULL DEFAULT 0,\\n                    documents INTEGER NOT NULL DEFAULT 0, consent INTEGER NOT NULL DEFAULT 0)"],["table","profiles","profiles","CREATE TABLE profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL)"],["table","provider_attempts","provider_attempts","CREATE TABLE provider_attempts(id TEXT PRIMARY KEY,\\n                   owner TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL,\\n                   data TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0,\\n                   day TEXT NOT NULL, response TEXT, stage TEXT NOT NULL, request_sha256 TEXT NOT NULL, UNIQUE(job_id,stage))"],["table","provider_retry_links","provider_retry_links","CREATE TABLE provider_retry_links(attempt_id TEXT PRIMARY KEY,\\n                   prior_attempt_id TEXT NOT NULL)"],["table","records","records","CREATE TABLE records(id TEXT PRIMARY KEY, owner TEXT NOT NULL,\\n                    kind TEXT NOT NULL, data TEXT NOT NULL)"],["table","research_blob_origins","research_blob_origins","CREATE TABLE research_blob_origins(id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, application_id TEXT NOT NULL, run_id TEXT NOT NULL,\\n                    intent_id TEXT NOT NULL, job_id TEXT NOT NULL, fence INTEGER NOT NULL)"],["table","research_blob_scopes","research_blob_scopes","CREATE TABLE research_blob_scopes(id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, application_id TEXT NOT NULL, run_id TEXT NOT NULL,\\n                    intent_id TEXT NOT NULL)"],["table","research_heads","research_heads","CREATE TABLE research_heads(application_id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, run_id TEXT NOT NULL, research INTEGER NOT NULL,\\n                    output INTEGER NOT NULL)"],["table","research_intents","research_intents","CREATE TABLE research_intents(id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, application_id TEXT NOT NULL, job_id TEXT NOT NULL,\\n                    fence INTEGER NOT NULL, run_id TEXT, state TEXT NOT NULL)"],["table","tickets","tickets","CREATE TABLE tickets(id TEXT PRIMARY KEY, data TEXT NOT NULL)"],["table","tombstones","tombstones","CREATE TABLE tombstones(id TEXT PRIMARY KEY, owner TEXT NOT NULL,\\n                    kind TEXT NOT NULL)"],["table","upload_intents","upload_intents","CREATE TABLE upload_intents(id TEXT PRIMARY KEY,\\n                    owner TEXT NOT NULL, state TEXT NOT NULL, blob_sha256 TEXT NOT NULL)"],["table","upload_receipts","upload_receipts","CREATE TABLE upload_receipts(id TEXT PRIMARY KEY,\\n                    device INTEGER NOT NULL, inode INTEGER NOT NULL)"]]}')
_CATALOG_SHA256 = '23032de43e59fd878490c0bc011d905e85e463cd0e96adf0fc6b4f54576569cb'
_SCHEMA_VERSION = 'copilot-physical-schema-v1'

_RESTORE_MESSAGES = {
    'RESTORE_STAGE_IO': 'Owned backup stage could not be used safely',
    'RESTORE_STAGE_CLEANUP': 'Owned backup stage could not be safely cleaned',
    'RESTORE_SQLITE_UNSUPPORTED': 'Required SQLite inspection protections are unavailable',
    'RESTORE_SQLITE_INVALID': 'Unsupported or malformed staged database',
    'RESTORE_SQLITE_LIMIT': 'Staged database inspection exceeds supported bounds',
}


def _restore_fail(code):
    raise EvidenceError(code, _RESTORE_MESSAGES[code]) from None


@dataclass(frozen=True)
class PhysicalCatalogObservation:
    """Caller-fabricable physical observation; excludes row integrity and authority."""
    archive_sha256: str
    canonical_database_sha256: str
    catalog_sha256: str
    source_schema_sha256: str
    schema_version: str
    table_count: int
    object_count: int


def _fd_identity(descriptor, *, directory=False):
    info = os.fstat(descriptor)
    if (info.st_uid != os.geteuid() or not 0 <= info.st_dev < 2**63
            or not 0 <= info.st_ino < 2**63):
        _restore_fail('RESTORE_STAGE_IO')
    result = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
              'mode': stat.S_IMODE(info.st_mode)}
    if directory:
        if not stat.S_ISDIR(info.st_mode) or result['mode'] != 0o700:
            _restore_fail('RESTORE_STAGE_IO')
    else:
        if (not stat.S_ISREG(info.st_mode) or result['mode'] != 0o600 or info.st_nlink != 1
                or not 0 <= info.st_size <= ArchiveLimits.per_payload_bytes):
            _restore_fail('RESTORE_STAGE_IO')
        result.update(nlink=info.st_nlink, size=info.st_size)
    return result


def _hash_descriptor(descriptor, size):
    result = hashlib.sha256()
    position = 0
    while position < size:
        part = os.pread(descriptor, min(_CHUNK, size - position), position)
        if not part:
            _restore_fail('RESTORE_STAGE_IO')
        result.update(part)
        position += len(part)
    if os.pread(descriptor, 1, size):
        _restore_fail('RESTORE_STAGE_IO')
    return result.hexdigest()


def _write_all(descriptor, content):
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            _restore_fail('RESTORE_STAGE_IO')
        offset += written


def _canonical_header(descriptor, size):
    header = os.pread(descriptor, 100, 0)
    if len(header) != 100 or header[:16] != b'SQLite format 3\0':
        _restore_fail('RESTORE_SQLITE_INVALID')
    encoded = int.from_bytes(header[16:18], 'big')
    page_size = 65536 if encoded == 1 else encoded
    if (not 512 <= page_size <= 65536 or page_size & (page_size - 1)
            or size < page_size or size % page_size or header[18:20] != b'\x01\x01'
            or int.from_bytes(header[44:48], 'big') not in (1, 2, 3, 4)
            or int.from_bytes(header[56:60], 'big') != 1):
        _restore_fail('RESTORE_SQLITE_INVALID')
    return page_size


@dataclass
class _CatalogStage:
    root: Path
    directory_fd: int
    identity: dict
    database_identity: dict | None = None
    reaped: bool = True


def _verify_stage_root(stage):
    if _fd_identity(stage.directory_fd, directory=True) != stage.identity:
        _restore_fail('RESTORE_STAGE_IO')
    info = os.stat(stage.root, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or (info.st_dev, info.st_ino) != (stage.identity['device'], stage.identity['inode'])
            or stat.S_IMODE(info.st_mode) != 0o700):
        _restore_fail('RESTORE_STAGE_IO')


def _check_database_path(directory_fd, database_fd):
    expected = _fd_identity(database_fd)
    actual = os.stat('evidence.sqlite3', dir_fd=directory_fd, follow_symlinks=False)
    if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != expected['uid']
            or stat.S_IMODE(actual.st_mode) != expected['mode'] or actual.st_nlink != 1
            or (actual.st_dev, actual.st_ino, actual.st_size)
            != (expected['device'], expected['inode'], expected['size'])):
        _restore_fail('RESTORE_STAGE_IO')
    return expected


def _verify_stage_database(stage, database_fd):
    _verify_stage_root(stage)
    info = _check_database_path(stage.directory_fd, database_fd)
    digest = _hash_descriptor(database_fd, info['size'])
    if dict(info, sha256=digest) != stage.database_identity:
        _restore_fail('RESTORE_STAGE_IO')
    if os.listdir(stage.directory_fd) != ['evidence.sqlite3']:
        _restore_fail('RESTORE_SQLITE_INVALID')


def _clean_catalog_stage(stage):
    # Never sweep unknown entries, replaced roots or files still used by a child.
    if not stage.reaped:
        _restore_fail('RESTORE_STAGE_CLEANUP')
    _verify_stage_root(stage)
    names = os.listdir(stage.directory_fd)
    allowed = {'evidence.sqlite3', 'evidence.sqlite3-wal',
               'evidence.sqlite3-shm', 'evidence.sqlite3-journal'}
    if len(names) > 4 or set(names) - allowed:
        _restore_fail('RESTORE_STAGE_CLEANUP')
    identities = {}
    for name in names:
        info = os.stat(name, dir_fd=stage.directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > ArchiveLimits.per_payload_bytes
                or stat.S_IMODE(info.st_mode) != 0o600):
            _restore_fail('RESTORE_STAGE_CLEANUP')
        if name == 'evidence.sqlite3' and stage.database_identity is not None:
            if (info.st_dev, info.st_ino) != (stage.database_identity['device'],
                                           stage.database_identity['inode']):
                _restore_fail('RESTORE_STAGE_CLEANUP')
        identities[name] = _identity(info)
    for name, identity in identities.items():
        if _identity(os.stat(name, dir_fd=stage.directory_fd, follow_symlinks=False)) != identity:
            _restore_fail('RESTORE_STAGE_CLEANUP')
        os.unlink(name, dir_fd=stage.directory_fd)
    if os.listdir(stage.directory_fd):
        _restore_fail('RESTORE_STAGE_CLEANUP')
    _verify_stage_root(stage)
    os.rmdir(stage.root)


@contextmanager
def _owned_catalog_stage():
    import tempfile
    try:
        root = Path(tempfile.mkdtemp(prefix='copilot-restore-catalog-', dir='/tmp'))
    except OSError:
        _restore_fail('RESTORE_STAGE_IO')
    directory_fd = None
    stage = None
    primary = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stage = _CatalogStage(root, directory_fd, _fd_identity(directory_fd, directory=True))
        _verify_stage_root(stage)
        yield stage
    except OSError:
        primary = 'RESTORE_STAGE_IO'
        _restore_fail('RESTORE_STAGE_IO')
    except BaseException as error:
        primary = getattr(error, 'code', None)
        raise
    finally:
        try:
            if stage is not None:
                try:
                    _clean_catalog_stage(stage)
                except (OSError, EvidenceError):
                    error = EvidenceError('RESTORE_STAGE_CLEANUP',
                                          _RESTORE_MESSAGES['RESTORE_STAGE_CLEANUP'])
                    error.primary_code = primary
                    raise error from None
        finally:
            if directory_fd is not None:
                try:
                    os.close(directory_fd)
                except OSError:
                    _restore_fail('RESTORE_STAGE_IO')


def _verify_archive_source(descriptor, before, path, digest=None):
    try:
        if _identity(os.fstat(descriptor)) != _identity(before):
            _deny('CHANGED')
        with _reselected_identity(path) as selected:
            if _identity(selected) != _identity(before):
                _deny('CHANGED')
        if digest is not None:
            try:
                actual = _hash_descriptor(descriptor, before.st_size)
            except EvidenceError:
                _deny('CHANGED')
            if actual != digest:
                _deny('CHANGED')
        if _identity(os.fstat(descriptor)) != _identity(before):
            _deny('CHANGED')
    except OSError:
        _deny('IO')


def _protocol_bytes(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > 4096:
        _restore_fail('RESTORE_SQLITE_LIMIT')
    return encoded


def _protocol_object(encoded):
    if not encoded or len(encoded) > 4096:
        _restore_fail('RESTORE_SQLITE_INVALID')
    try:
        value = json.loads(encoded.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: _restore_fail('RESTORE_SQLITE_INVALID'))
    except (ValueError, UnicodeError, RecursionError, EvidenceError):
        _restore_fail('RESTORE_SQLITE_INVALID')
    if type(value) is not dict:
        _restore_fail('RESTORE_SQLITE_INVALID')
    return value


def _protocol_keys(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        _restore_fail('RESTORE_SQLITE_INVALID')


def _protocol_integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        _restore_fail('RESTORE_SQLITE_INVALID')


def _protocol_digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        _restore_fail('RESTORE_SQLITE_INVALID')


def _check_request(value, stage_fd, database_fd):
    _protocol_keys(value, 'version parent_pid child_pid stage_identity database_identity '
                          'declared_schema_sha256')
    for key in ('version', 'parent_pid', 'child_pid'):
        _protocol_integer(value[key])
    if (value['version'] != 1 or value['parent_pid'] != os.getppid()
            or value['child_pid'] != os.getpid()):
        _restore_fail('RESTORE_SQLITE_INVALID')
    _protocol_keys(value['stage_identity'], 'device inode uid mode')
    _protocol_keys(value['database_identity'], 'device inode uid mode nlink size sha256')
    for item in (value['stage_identity'], value['database_identity']):
        for key, scalar in item.items():
            if key != 'sha256':
                _protocol_integer(scalar)
    _protocol_digest(value['declared_schema_sha256'])
    _protocol_digest(value['database_identity']['sha256'])
    if (value['stage_identity'] != _fd_identity(stage_fd, directory=True)
            or {k: v for k, v in value['database_identity'].items() if k != 'sha256'}
            != _check_database_path(stage_fd, database_fd)
            or value['database_identity']['sha256']
            != _hash_descriptor(database_fd, value['database_identity']['size'])
            or os.listdir(stage_fd) != ['evidence.sqlite3']):
        _restore_fail('RESTORE_STAGE_IO')


def _check_response(value, request):
    if value.get('status') == 'error':
        _protocol_keys(value, 'version status code')
        if type(value['version']) is not int or value['version'] != 1:
            _restore_fail('RESTORE_SQLITE_INVALID')
        if type(value['code']) is not str or value['code'] not in _RESTORE_MESSAGES:
            _restore_fail('RESTORE_SQLITE_INVALID')
        _restore_fail(value['code'])
    _protocol_keys(value, 'version status database_sha256 catalog_sha256 declared_schema_sha256 '
                          'schema_version table_count object_count')
    for key in ('version', 'table_count', 'object_count'):
        _protocol_integer(value[key])
    for key in ('database_sha256', 'catalog_sha256', 'declared_schema_sha256'):
        _protocol_digest(value[key])
    if (value['version'] != 1 or value['status'] != 'ok'
            or value['database_sha256'] != request['database_identity']['sha256']
            or value['catalog_sha256'] != _CATALOG_SHA256
            or value['declared_schema_sha256'] != request['declared_schema_sha256']
            or value['declared_schema_sha256'] != _CATALOG_SHA256
            or value['schema_version'] != _SCHEMA_VERSION
            or value['table_count'] != 26 or value['object_count'] != 57):
        _restore_fail('RESTORE_SQLITE_INVALID')

@dataclass
class _CatalogPolicy:
    page_count: int
    deadline: float
    phase: str = 'catalog'
    progress_calls: int = 0
    pragma: tuple | None = None


def _install_sqlite_controls(connection, policy):
    import sqlite3
    import time
    configs = {'TRUSTED_SCHEMA': False, 'DEFENSIVE': True,
               'ENABLE_LOAD_EXTENSION': False, 'ENABLE_TRIGGER': False, 'ENABLE_VIEW': False}
    bounds = {'LENGTH': 1048576, 'SQL_LENGTH': 65536, 'COLUMN': 64, 'EXPR_DEPTH': 100,
              'COMPOUND_SELECT': 8, 'VDBE_OP': 100000, 'FUNCTION_ARG': 32,
              'VARIABLE_NUMBER': 32, 'LIKE_PATTERN_LENGTH': 1024, 'TRIGGER_DEPTH': 0,
              'ATTACHED': 0, 'WORKER_THREADS': 0}
    try:
        for name, expected in configs.items():
            constant = getattr(sqlite3, 'SQLITE_DBCONFIG_' + name)
            connection.setconfig(constant, expected)
            if connection.getconfig(constant) != expected:
                _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
        for name, expected in bounds.items():
            constant = getattr(sqlite3, 'SQLITE_LIMIT_' + name)
            connection.setlimit(constant, expected)
            actual = connection.getlimit(constant)
            if not 0 <= actual <= expected:
                _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
    except (AttributeError, sqlite3.Error, ValueError):
        _restore_fail('RESTORE_SQLITE_UNSUPPORTED')

    def authorize(action, first, second, database, origin):
        if origin is not None:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT and policy.phase == 'catalog':
            return sqlite3.SQLITE_OK
        if (action == sqlite3.SQLITE_READ and policy.phase == 'catalog'
                and database == 'main' and first in ('sqlite_schema', 'sqlite_master')
                and second in ('type', 'name', 'tbl_name', 'rootpage', 'sql')):
            return sqlite3.SQLITE_OK
        if (action == sqlite3.SQLITE_PRAGMA and policy.phase == 'metadata'
                and database == 'main' and (first, second) == policy.pragma):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def progress():
        policy.progress_calls += 1
        return int(policy.progress_calls >= 20000 or time.monotonic() >= policy.deadline)

    connection.set_authorizer(authorize)
    connection.set_progress_handler(progress, 1000)


def _catalog_encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _check_policy_deadline(policy):
    import time
    if time.monotonic() >= policy.deadline:
        _restore_fail('RESTORE_SQLITE_LIMIT')


def _bounded_rows(cursor, aggregate, *, policy):
    rows = []
    for row in cursor:
        _check_policy_deadline(policy)
        if len(rows) >= 128:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        if any(type(scalar) not in (str, int, type(None)) for scalar in row):
            _restore_fail('RESTORE_SQLITE_INVALID')
        for scalar in row:
            if type(scalar) is str and len(scalar.encode('utf-8')) > 65536:
                _restore_fail('RESTORE_SQLITE_LIMIT')
        encoded = _catalog_encoded(list(row))
        aggregate[0] += len(encoded)
        if aggregate[0] > 262144:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        rows.append(list(row))
    return rows


def _read_supported_catalog(connection, policy):
    _check_policy_deadline(policy)
    rows = _bounded_rows(connection.execute(
        'SELECT type,name,tbl_name,rootpage,sql FROM main.sqlite_schema'), [0], policy=policy)
    _check_policy_deadline(policy)
    objects = []
    for row in rows:
        if (len(row) != 5 or row[0] not in ('table', 'index')
                or type(row[1]) is not str or type(row[2]) is not str
                or len(row[1]) > 128 or len(row[2]) > 128
                or type(row[3]) is not int or not 0 < row[3] <= policy.page_count
                or (row[4] is not None and type(row[4]) is not str)):
            _restore_fail('RESTORE_SQLITE_INVALID')
        objects.append([row[0], row[1], row[2], row[4]])
    objects.sort()
    if objects != _SUPPORTED_CATALOG['objects']:
        _restore_fail('RESTORE_SQLITE_INVALID')
    digest = hashlib.sha256(_catalog_encoded({'version': _SCHEMA_VERSION,
                                             'objects': objects})).hexdigest()
    if digest != _CATALOG_SHA256:
        _restore_fail('RESTORE_SQLITE_INVALID')
    policy.phase = 'metadata'
    aggregate = [0]

    def metadata(name, target):
        # Names come exclusively from the closed trusted literal catalog.
        _check_policy_deadline(policy)
        policy.pragma = (name, target)
        try:
            rows = _bounded_rows(connection.execute(f'PRAGMA main.{name}("{target}")'),
                                 aggregate, policy=policy)
            _check_policy_deadline(policy)
            return rows
        finally:
            policy.pragma = None

    for table, expected in _SUPPORTED_CATALOG['metadata'].items():
        actual = {name: metadata(name, table)
                  for name in ('table_xinfo', 'index_list', 'foreign_key_list')}
        actual['index_xinfo'] = {name: metadata('index_xinfo', name)
                                 for name in expected['index_xinfo']}
        if actual != expected:
            _restore_fail('RESTORE_SQLITE_INVALID')
    return digest


def _verify_child_descriptors(stage_fd, database_fd, request_fd, response_fd):
    import fcntl
    descriptors = (stage_fd, database_fd, request_fd, response_fd)
    if any(type(fd) is not int or fd < 0 for fd in descriptors) or len(set(descriptors)) != 4:
        _restore_fail('RESTORE_SQLITE_INVALID')
    for descriptor, expected_mode, file_type in (
            (stage_fd, os.O_RDONLY, stat.S_ISDIR),
            (database_fd, os.O_RDONLY, stat.S_ISREG),
            (request_fd, os.O_RDONLY, stat.S_ISFIFO),
            (response_fd, os.O_WRONLY, stat.S_ISFIFO)):
        if (fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != expected_mode
                or not file_type(os.fstat(descriptor).st_mode)):
            _restore_fail('RESTORE_SQLITE_INVALID')


def _catalog_child(stage_fd, database_fd, request_fd, response_fd):
    """Fixed four-FD target; called only by the private isolated interpreter bootstrap."""
    connection = None
    response = None
    try:
        import resource
        import time
        try:
            for limit, expected in ((resource.RLIMIT_AS, 512 * 1024 * 1024),
                                    (resource.RLIMIT_CORE, 0)):
                resource.setrlimit(limit, (expected, expected))
                if resource.getrlimit(limit) != (expected, expected):
                    _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
        except (OSError, ValueError):
            _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
        _verify_child_descriptors(stage_fd, database_fd, request_fd, response_fd)
        request_bytes = bytearray()
        while True:
            part = os.read(request_fd, min(4097 - len(request_bytes), 4096))
            if not part:
                break
            request_bytes.extend(part)
            if len(request_bytes) > 4096:
                _restore_fail('RESTORE_SQLITE_LIMIT')
        request = _protocol_object(bytes(request_bytes))
        _check_request(request, stage_fd, database_fd)
        page_size = _canonical_header(database_fd, request['database_identity']['size'])
        if request['declared_schema_sha256'] != _CATALOG_SHA256:
            _restore_fail('RESTORE_SQLITE_INVALID')
        # Limits and FD/request validation precede this first SQLite import.
        import sqlite3
        from urllib.parse import quote
        uri = 'file:' + quote(f'/proc/self/fd/{stage_fd}/evidence.sqlite3', safe='/') + '?mode=ro'
        _check_database_path(stage_fd, database_fd)
        connection = sqlite3.connect(uri, uri=True, timeout=0, detect_types=0,
                                     autocommit=True, cached_statements=0)
        _check_database_path(stage_fd, database_fd)
        policy = _CatalogPolicy(request['database_identity']['size'] // page_size,
                                time.monotonic() + 5)
        _install_sqlite_controls(connection, policy)
        digest = _read_supported_catalog(connection, policy)
        connection.close()
        connection = None
        _check_request(request, stage_fd, database_fd)
        response = {'version': 1, 'status': 'ok',
                    'database_sha256': request['database_identity']['sha256'],
                    'catalog_sha256': digest,
                    'declared_schema_sha256': request['declared_schema_sha256'],
                    'schema_version': _SCHEMA_VERSION, 'table_count': 26, 'object_count': 57}
    except EvidenceError as error:
        code = error.code if error.code in _RESTORE_MESSAGES else 'RESTORE_SQLITE_INVALID'
        response = {'version': 1, 'status': 'error', 'code': code}
    except (ImportError, AttributeError, ValueError):
        response = {'version': 1, 'status': 'error', 'code': 'RESTORE_SQLITE_UNSUPPORTED'}
    except MemoryError:
        response = {'version': 1, 'status': 'error', 'code': 'RESTORE_SQLITE_LIMIT'}
    except Exception:
        response = {'version': 1, 'status': 'error', 'code': 'RESTORE_SQLITE_INVALID'}
    finally:
        if connection is not None:
            connection.close()
        try:
            if response is not None:
                _write_all(response_fd, _protocol_bytes(response))
        finally:
            for descriptor in (stage_fd, database_fd, request_fd, response_fd):
                os.close(descriptor)


def _teardown_catalog_child(process):
    """One TERM budget followed by one shared KILL/reap budget, never retried."""
    import subprocess
    if process.poll() is not None:
        process.wait(timeout=0)
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
            return True
        except subprocess.TimeoutExpired:
            return False


def _exchange_catalog_child(stage, database_fd, request, interruption):
    import selectors
    import subprocess
    import sys
    import time
    descriptors = []
    process = None
    selector = None
    try:
        if interruption[0]:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        request_read, request_write = os.pipe2(os.O_CLOEXEC)
        descriptors.extend((request_read, request_write))
        response_read, response_write = os.pipe2(os.O_CLOEXEC)
        descriptors.extend((response_read, response_write))
        backend = str(Path(__file__).resolve().parents[1])
        bootstrap = (f'import sys;sys.path.insert(0,{backend!r});'
                     'from copilot.restore import _catalog_child;'
                     f'_catalog_child({stage.directory_fd},{database_fd},'
                     f'{request_read},{response_write})')
        deadline = time.monotonic() + 10
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   pass_fds=(stage.directory_fd, database_fd,
                                             request_read, response_write))
        stage.reaped = False
        os.close(request_read)
        descriptors.remove(request_read)
        os.close(response_write)
        descriptors.remove(response_write)
        if interruption[0]:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        request = dict(request, child_pid=process.pid)
        encoded = _protocol_bytes(request)
        position = 0
        response = bytearray()
        os.set_blocking(request_write, False)
        os.set_blocking(response_read, False)
        selector = selectors.DefaultSelector()
        selector.register(request_write, selectors.EVENT_WRITE)
        selector.register(response_read, selectors.EVENT_READ)
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if interruption[0] or remaining <= 0:
                _restore_fail('RESTORE_SQLITE_LIMIT')
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.fd == request_write:
                    try:
                        written = os.write(request_write, encoded[position:])
                    except BlockingIOError:
                        continue
                    if written <= 0:
                        _restore_fail('RESTORE_STAGE_IO')
                    position += written
                    if position == len(encoded):
                        selector.unregister(request_write)
                        os.close(request_write)
                        descriptors.remove(request_write)
                else:
                    try:
                        part = os.read(response_read, 4097 - len(response))
                    except BlockingIOError:
                        continue
                    if not part:
                        eof = True
                        selector.unregister(response_read)
                    else:
                        response.extend(part)
                        if len(response) > 4096:
                            _restore_fail('RESTORE_SQLITE_LIMIT')
        remaining = deadline - time.monotonic()
        if interruption[0] or remaining <= 0 or position != len(encoded):
            _restore_fail('RESTORE_SQLITE_LIMIT')
        while True:
            remaining = deadline - time.monotonic()
            if interruption[0] or remaining <= 0:
                _restore_fail('RESTORE_SQLITE_LIMIT')
            try:
                status = process.wait(timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                continue
        stage.reaped = True
        if time.monotonic() >= deadline:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        if interruption[0] or status != 0:
            _restore_fail('RESTORE_SQLITE_INVALID')
        value = _protocol_object(bytes(response))
        _check_response(value, request)
        if time.monotonic() >= deadline:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        return value
    finally:
        try:
            if process is not None and not stage.reaped:
                stage.reaped = _teardown_catalog_child(process)
        finally:
            if selector is not None:
                selector.close()
            for descriptor in descriptors:
                os.close(descriptor)


def _observe_archive_catalog(archive_path: Path, expected_sha256: str, *,
                             limits: ArchiveLimits = ArchiveLimits()) -> PhysicalCatalogObservation:
    """Observe copied physical catalog only, never logical validity or restore readiness."""
    import signal
    import sys
    import threading
    if sys.platform != 'linux':
        _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
    if threading.current_thread() is not threading.main_thread():
        _restore_fail('RESTORE_SQLITE_UNSUPPORTED')
    _digest(expected_sha256)
    _checked_limits(limits)
    interruption = [False]
    previous = {}
    database_fd = None

    def interrupted(_signal, _frame):
        interruption[0] = True

    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.getsignal(name)
            signal.signal(name, interrupted)
        if interruption[0]:
            _restore_fail('RESTORE_SQLITE_LIMIT')
        with _selected_archive(archive_path) as (descriptor, before, path):
            with _owned_catalog_stage() as stage:
                try:
                    database_fd = os.open('evidence.sqlite3', os.O_WRONLY | os.O_CREAT
                                          | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                          0o600, dir_fd=stage.directory_fd)
                    stage.database_identity = _fd_identity(database_fd)
                    inventory = _scan_selected(descriptor, before, expected_sha256, limits,
                                               database_sink=lambda part: _write_all(database_fd, part))
                    os.fsync(database_fd)
                    os.close(database_fd)
                    database_fd = None
                    database_fd = os.open('evidence.sqlite3', os.O_RDONLY | os.O_NOFOLLOW
                                          | os.O_CLOEXEC, dir_fd=stage.directory_fd)
                    identity = _fd_identity(database_fd)
                    if (identity['device'], identity['inode']) != (
                            stage.database_identity['device'], stage.database_identity['inode']):
                        _restore_fail('RESTORE_STAGE_IO')
                    if identity['size'] != inventory.entries[0].size:
                        _restore_fail('RESTORE_STAGE_IO')
                    stage.database_identity = dict(identity, sha256=_hash_descriptor(
                        database_fd, identity['size']))
                    if stage.database_identity['sha256'] != inventory.entries[0].sha256:
                        _restore_fail('RESTORE_STAGE_IO')
                    _canonical_header(database_fd, identity['size'])
                    _verify_archive_source(descriptor, before, path)
                    _verify_stage_database(stage, database_fd)
                    request = {'version': 1, 'parent_pid': os.getpid(),
                               'child_pid': 0, 'stage_identity': stage.identity,
                               'database_identity': stage.database_identity,
                               'declared_schema_sha256': inventory.source_schema_sha256}
                    response = _exchange_catalog_child(stage, database_fd, request, interruption)
                    _verify_stage_database(stage, database_fd)
                    _verify_archive_source(descriptor, before, path, inventory.archive_sha256)
                    if interruption[0]:
                        _restore_fail('RESTORE_SQLITE_LIMIT')
                    result = PhysicalCatalogObservation(inventory.archive_sha256,
                        stage.database_identity['sha256'], response['catalog_sha256'],
                        inventory.source_schema_sha256, response['schema_version'],
                        response['table_count'], response['object_count'])
                finally:
                    if database_fd is not None:
                        os.close(database_fd)
                        database_fd = None
        if interruption[0]:
            _restore_fail('RESTORE_SQLITE_LIMIT')
    except OSError:
        _deny('IO')
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
    if interruption[0]:
        _restore_fail('RESTORE_SQLITE_LIMIT')
    return result
