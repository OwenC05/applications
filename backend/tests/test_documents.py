import io
import subprocess

import pytest
from pypdf import PdfWriter

from copilot import documents
from copilot.contracts import EvidenceError
from copilot.store import Store


@pytest.fixture
def setup(tmp_path):
    s = Store(tmp_path)
    return s, s.create_profile('Synthetic', ['tech']).id


def test_utf8_normalization_and_original(setup):
    s, p = setup
    content = 'café\r\nline\rthird'.encode()
    source = documents.ingest(s, p, 'notes.md', 'text/markdown', content)
    assert s.list_units(p, source.id)[0].text == 'café\nline\nthird'
    assert s.read_source(p, source.id) == content


@pytest.mark.parametrize('filename,media,content', [
    ('x.txt', 'text/plain', b'\xff'), ('x.txt', 'text/plain', b' '),
    ('x.docx', 'application/octet-stream', b'fake'),
    ('x.pdf', 'application/pdf', b'%PDF-invalid'),
    ('x.txt', 'text/plain', b'\x00binary'),
])
def test_invalid_upload_leaves_no_source(setup, filename, media, content):
    s, p = setup
    with pytest.raises(EvidenceError):
        documents.ingest(s, p, filename, media, content)
    assert s.list_sources(p) == []
    assert list(s.blobs.iterdir()) == []


def test_scanned_encrypted_and_page_limit(setup):
    s, p = setup
    for encrypt, pages in [(False, 1), (True, 1), (False, 101)]:
        w = PdfWriter()
        for _ in range(pages):
            w.add_blank_page(width=100, height=100)
        if encrypt:
            w.encrypt('secret')
        b = io.BytesIO()
        w.write(b)
        with pytest.raises(EvidenceError):
            documents.ingest(s, p, 'bad.pdf', 'application/pdf', b.getvalue())
    assert not s.list_sources(p)


def test_parser_deadline_is_safe(setup, monkeypatch):
    s, p = setup
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 30
        assert kwargs['stderr'] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args[0], 30)
    monkeypatch.setattr(documents.subprocess, 'run', timeout)
    with pytest.raises(EvidenceError) as exc:
        documents.ingest(s, p, 'private-name.pdf', 'application/pdf', b'%PDF-1.7')
    assert 'private-name' not in str(exc.value)
    assert not s.list_sources(p)


def text_pdf():
    """Minimal synthetic PDF; no personal source fixture or extra dependency."""
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    stream = b'BT /F1 12 Tf 20 250 Td (Synthetic analyst example) Tj ET'
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    result = b'%PDF-1.4\n'
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(n).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(result)
    result += b'xref\n0 6\n0000000000 65535 f \n'
    for offset in offsets[1:]:
        result += f'{offset:010d} 00000 n \n'.encode()
    result += b'trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
    return result


def test_real_positive_text_pdf_and_caps(setup):
    s, p = setup
    source = documents.ingest(s, p, 'synthetic.pdf', 'application/pdf', text_pdf())
    unit = s.list_units(p, source.id)[0]
    assert unit.page == 1 and unit.text == 'Synthetic analyst example'
    with pytest.raises(EvidenceError) as exc:
        documents.ingest(s, p, 'large.txt', 'text/plain', b'a' * (10 * 1024 * 1024 + 1))
    assert exc.value.code == 'LIMIT_EXCEEDED'
    with pytest.raises(EvidenceError):
        documents.ingest(s, p, 'long.txt', 'text/plain', b'a' * 1_000_001)


def test_browser_markdown_generic_mime_still_bounded_utf8(tmp_path):
    from copilot.store import Store
    store = Store(tmp_path)
    profile = store.create_profile('Synthetic browser', ['tech'])
    source = documents.ingest(store, profile.id, 'notes.md', 'application/octet-stream', b'# Synthetic notes\nPython testing')
    assert source.media_type == 'text/markdown'
    assert store.list_units(profile.id, source.id)[0].text == '# Synthetic notes\nPython testing'
