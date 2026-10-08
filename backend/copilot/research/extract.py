"""Static source extraction. No script execution, URL loading or fact promotion."""
import json
import subprocess
import sys
from pathlib import Path

MAX_TEXT = 1_000_000
MAX_BYTES = 2 * 1024 * 1024
HTML_HELPER = Path(__file__).with_name('_parse_html.py')


class ExtractionError(ValueError):
    pass



def extract(content: bytes, media_type: str, *, timeout: float) -> tuple[str, str]:
    """Return canonical text and version; empty/JS-only/scanned content fails closed.

    Static text may be incomplete even when nonempty. The caller must separately assess
    company/exact-role coverage. HTML and PDF execute in killable, resource-limited
    Linux children; parser work limits are not a replacement for the caller deadline.
    """
    if timeout <= 0:
        raise ExtractionError('Extraction deadline expired')
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ExtractionError('Static source byte limit exceeded')
    if media_type == 'text/html':
        if sys.platform != 'linux':
            raise ExtractionError('Bounded HTML parsing requires Linux')
        try:
            result = subprocess.run(
                [sys.executable, '-I', str(HTML_HELPER)],
                input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, 15), check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 4 * MAX_TEXT:
                raise ValueError()
            text = result.stdout.decode('utf-8')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # subprocess.run kills AND waits for the timed-out child before returning.
            raise ExtractionError('Static HTML extraction failed or exceeded limits') from None
        version = 'stdlib-html-isolated-text-v2'
    elif media_type == 'application/pdf':
        if sys.platform != 'linux' or not content.startswith(b'%PDF-'):
            raise ExtractionError('Bounded text PDF extraction requires Linux and a PDF')
        try:
            result = subprocess.run(
                [sys.executable, '-I', str(Path(__file__).parents[1] / '_parse_pdf.py')],
                input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, 30), check=False,
            )
            units = json.loads(result.stdout) if result.returncode == 0 else None
            if (not isinstance(units, list) or not 1 <= len(units) <= 100
                    or any(not isinstance(unit, str) for unit in units)):
                raise ValueError()
            text = '\n\n'.join(units)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            raise ExtractionError('Text PDF extraction failed or exceeded limits') from None
        version = 'pypdf-6.19.0-page-text-v1'
    elif media_type in ('text/plain', 'text/markdown'):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeError:
            raise ExtractionError('Only UTF-8 static text is supported') from None
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        version = 'utf8-newlines-v1'
    else:
        raise ExtractionError('Unsupported public source media type')
    if not text.strip() or '\x00' in text or len(text) > MAX_TEXT:
        raise ExtractionError('No supported static text or extracted text exceeds limit')
    return text, version
