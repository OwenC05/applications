"""Explicit uploads only. No filesystem discovery or URL acquisition."""
import json
import subprocess
import sys
from pathlib import Path

from .contracts import EvidenceError, Source
from .store import Store

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARACTERS = 1_000_000
PDF_WALL_SECONDS = 30


def ingest(store: Store, profile_id: str, filename: str, media_type: str, content: bytes) -> Source:
    # Reject foreign owners before spending parser resources.
    store.validate_snapshot(profile_id, next((p.revision for p in store.list_profiles() if p.id == profile_id), -1))
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise EvidenceError('LIMIT_EXCEEDED', 'Upload limit is 10 MiB', 413)
    suffix = Path(filename).suffix.lower() if isinstance(filename, str) else ''
    if media_type in ('', 'application/octet-stream'):
        media_type = {'.txt': 'text/plain', '.md': 'text/markdown', '.pdf': 'application/pdf'}.get(suffix, media_type)
    if media_type in ('text/plain', 'text/markdown') and suffix in ('.txt', '.md'):
        try:
            text = content.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        except UnicodeDecodeError:
            raise EvidenceError('INVALID_INPUT', 'Document must use UTF-8', 400) from None
        if '\x00' in text or not text.strip():
            raise EvidenceError('INVALID_INPUT', 'Document has no supported text', 400)
        if len(text) > MAX_CHARACTERS:
            raise EvidenceError('LIMIT_EXCEEDED', 'Extracted text limit is 1 million characters', 413)
        units = [text]
        version = 'utf8-newlines-v1'
    elif media_type == 'application/pdf' and suffix == '.pdf':
        if sys.platform != 'linux':
            raise EvidenceError('INVALID_INPUT', 'Bounded PDF parsing requires Linux', 400)
        if not content.startswith(b'%PDF-'):
            raise EvidenceError('INVALID_INPUT', 'Invalid PDF document', 400)
        helper = Path(__file__).with_name('_parse_pdf.py')
        try:
            result = subprocess.run([sys.executable, '-I', str(helper)], input=content,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=PDF_WALL_SECONDS, check=False)
            if result.returncode != 0:
                raise ValueError()
            units = json.loads(result.stdout)
            if not isinstance(units, list) or not units or len(units) > 100 or any(not isinstance(t, str) for t in units) or sum(map(len, units)) > MAX_CHARACTERS or not any(t.strip() for t in units):
                raise ValueError()
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            raise EvidenceError('INVALID_INPUT', 'PDF extraction failed or exceeded limits; use a text PDF with at most 100 pages', 400) from None
        version = 'pypdf-6.19.0-page-text-v1'
    else:
        raise EvidenceError('INVALID_INPUT', 'Only UTF-8 TXT, MD and text PDF uploads are supported', 400)
    return store.add_source(profile_id, filename, media_type, content, units, version)
