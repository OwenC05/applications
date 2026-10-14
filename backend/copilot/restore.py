"""Inactive structural backup scanner, NOT logical validation or restore authority.

Inventories and declared provenance are caller-fabricable data. This module does
not open SQLite, validate application relationships, extract files, install a
capture, or authorize destination preparation. Hashes prove byte selection, not
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
            part = os.read(self.descriptor, amount - len(value))
            if not part:
                _deny('INVALID')
            value.extend(part)
        self.position += amount
        self.digest.update(value)
        return bytes(value)

    def payload(self, size, *, buffer=False):
        digest = hashlib.sha256()
        output = bytearray() if buffer else None
        remaining = size
        while remaining:
            part = self.read(min(remaining, _CHUNK))
            remaining -= len(part)
            digest.update(part)
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


def _scan_archive(archive_path: Path, expected_sha256: str, *,
                  limits: ArchiveLimits = ArchiveLimits()) -> StructuralInventory:
    """Scan structural bytes only; never treat the returned data as trusted restore input."""
    _digest(expected_sha256)
    if type(limits) is not ArchiveLimits:
        _deny('LIMIT')
    # Frozen dataclasses are convenience, not validation authority.
    limits.__post_init__()
    try:
        with _selected_archive(archive_path) as (descriptor, before, _):
            upper_payload_end = (limits.manifest_bytes + limits.aggregate_payload_bytes
                                 + (limits.entries + 1) * (2 * _BLOCK - 1))
            # Two end blocks precede, rather than replace, record alignment.
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
            for entry in entries:
                name, size = _header(reader)
                if name != entry.path or size != entry.size:
                    _deny('INVALID')
                if reader.payload(size) != entry.sha256:
                    _deny('HASH')
            # Accept exactly the two-block ending or standard tar record padding.
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
    except OSError:
        _deny('IO')
