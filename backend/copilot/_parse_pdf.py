"""Fresh, resource-limited PDF process. Protocol contains no original filesystem path."""
import json
import resource
import sys

# Set before importing the parser or opening untrusted input.
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (30, 30))


def main():
    import io

    from pypdf import PdfReader

    content = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise ValueError()
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or len(reader.pages) > 100:
        raise ValueError()
    units = []
    count = 0
    for page in reader.pages:
        text = (page.extract_text() or '').replace('\r\n', '\n').replace('\r', '\n')
        count += len(text)
        if count > 1_000_000:
            raise ValueError()
        units.append(text)
    if not units or not any(t.strip() for t in units):
        raise ValueError()
    sys.stdout.write(json.dumps(units))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never expose parser diagnostics, filename, document contents or metadata.
        sys.exit(2)
