"""Independent raw TAR fixtures: synthetic only; no archive library normalization."""
import hashlib
import json

SOURCE = '11111111-1111-4111-8111-111111111111'
OWNER = '22222222-2222-4222-8222-222222222222'
DB = b'not a database: structural validation is not logical validation'
BLOB = b'synthetic original\r\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def manifest():
    entries = [
        {'path': 'evidence.sqlite3', 'type': 'file', 'size': len(DB), 'sha256': digest(DB)},
        {'path': f'blobs/{SOURCE}', 'type': 'file', 'size': len(BLOB), 'sha256': digest(BLOB)},
    ]
    return {
        'format': 'copilot-backup', 'version': 1, 'backup_id': OWNER,
        'source_schema_sha256': 'a' * 64,
        'application_compatibility': 'application-copilot-evidence-0.2.0',
        'model_lock_sha256': 'b' * 64, 'installation_id': SOURCE,
        'entries': entries,
        'originals': [{'source_id': SOURCE, 'owner': OWNER, 'source_kind': 'source',
                       'path': f'blobs/{SOURCE}', 'sha256': digest(BLOB)}],
        'snapshot': {'method': 'offline-stage-backup-v1', 'raw_capture_sha256': 'c' * 64,
                     'canonical_database_sha256': digest(DB)},
    }


def checksum(header):
    result = bytearray(header)
    result[148:156] = b' ' * 8
    result[148:156] = f'{sum(result):06o}\0 '.encode('ascii')
    return bytes(result)


def header(name, size, *, typeflag=b'0', changes=()):
    raw = bytearray(512)
    name = name.encode('utf-8') if isinstance(name, str) else name
    raw[:len(name)] = name
    raw[100:108] = b'0000600\0'
    raw[108:116] = b'0000000\0'
    raw[116:124] = b'0000000\0'
    raw[124:136] = f'{size:011o}\0'.encode('ascii')
    raw[136:148] = b'00000000000\0'
    raw[156:157] = typeflag
    raw[257:263] = b'ustar\0'
    raw[263:265] = b'00'
    for offset, value in changes:
        raw[offset:offset + len(value)] = value
    return checksum(raw)


def member(name, data, **options):
    return header(name, len(data), **options) + data + b'\0' * (-len(data) % 512)


def archive(value=None, *, manifest_bytes=None, payloads=None, tail=None):
    value = manifest() if value is None else value
    encoded = json.dumps(value, separators=(',', ':')).encode() if manifest_bytes is None else manifest_bytes
    payloads = [('evidence.sqlite3', DB), (f'blobs/{SOURCE}', BLOB)] if payloads is None else payloads
    return (member('manifest.json', encoded)
            + b''.join(member(path, data) for path, data in payloads)
            + (b'\0' * 1024 if tail is None else tail))
