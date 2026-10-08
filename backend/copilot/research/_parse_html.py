"""Isolated Linux HTML text parser; stdin bytes, bounded UTF-8 stdout, no paths/URLs."""
import resource
import sys

# Install ceilings before loading or parsing attacker-controlled input.
resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

from html.parser import HTMLParser  # noqa: E402

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 1_000_000
MAX_DEPTH = 512
MAX_EVENTS = 200_000
HIDDEN = {'script', 'style', 'template', 'noscript', 'head'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
        'param', 'source', 'track', 'wbr'}
BREAKS = {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr', 'section'}


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []
        self.characters = 0
        self.events = 0

    def _event(self):
        self.events += 1
        if self.events > MAX_EVENTS:
            raise ValueError('HTML work limit')

    def _emit(self, data):
        self.characters += len(data)
        if self.characters > MAX_TEXT:
            raise ValueError('HTML text limit')
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self._event()
        if self.hidden or tag in HIDDEN:
            if tag not in VOID:
                if len(self.hidden) >= MAX_DEPTH:
                    raise ValueError('HTML nesting limit')
                self.hidden.append(tag)
        elif tag in BREAKS:
            self._emit('\n')

    def handle_endtag(self, tag):
        self._event()
        if self.hidden:
            # At most MAX_DEPTH entries: mismatched end tags cannot trigger unbounded
            # quadratic membership scans. The process deadline remains authoritative.
            if tag in self.hidden:
                del self.hidden[self.hidden.index(tag):]
        elif tag in BREAKS:
            self._emit('\n')

    def handle_data(self, data):
        self._event()
        if not self.hidden:
            self._emit(data)

    def handle_comment(self, data):
        self._event()


def main():
    content = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('HTML byte limit')
    text = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    parser = HTMLText()
    parser.feed(text)
    parser.close()
    lines = (' '.join(line.split()) for line in ''.join(parser.parts).splitlines())
    text = '\n'.join(line for line in lines if line)
    if not text.strip() or '\x00' in text or len(text) > MAX_TEXT:
        raise ValueError('No supported static HTML text')
    sys.stdout.buffer.write(text.encode('utf-8'))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No untrusted content or diagnostics cross the parser boundary.
        sys.exit(2)
