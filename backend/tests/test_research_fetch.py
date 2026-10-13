"""Synthetic static acquisition tests; no external sites or credentials."""
import gzip
import hashlib
import http.client
import io
import json
import socket
import ssl
import subprocess
import time

import pytest

from copilot.research import fetch
from copilot.research.extract import ExtractionError, extract


class Response:
    def __init__(self, body=b'Confirmed public role', status=200, headers=None):
        self.status = status
        self.headers = {'Content-Type': 'text/plain', **(headers or {})}
        self.stream = io.BytesIO(body)

    def getheader(self, key):
        return self.headers.get(key)

    def read(self, size):
        return self.stream.read(size)

    def close(self):
        self.stream.close()


@pytest.fixture
def transport(monkeypatch):
    calls, replies = [], []
    monkeypatch.setattr(fetch, '_resolve', lambda host, deadline: ['93.184.216.34'])

    class Connection:
        def __init__(self, host, address, deadline):
            self.closed = False
            self.call = {'host': host, 'address': address, 'deadline': deadline}
            calls.append(self.call)

        def request(self, method, target, headers):
            self.call.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return replies.pop(0) if replies else Response()

        def close(self):
            self.call['closed'] = True

    monkeypatch.setattr(fetch, '_PinnedHTTPS', Connection)
    return calls, replies


def test_failed_extractions_consume_shared_response_body_budget(transport):
    calls, replies = transport
    budget = fetch.FetchBudget(max_bytes=2 * fetch.MAX_BYTES)
    for _ in range(2):
        replies.append(Response(b'x' * fetch.MAX_BYTES,
                                headers={'Content-Type': 'application/unsupported',
                                         'Content-Length': str(fetch.MAX_BYTES)}))
        with pytest.raises(fetch.FetchError):
            fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert budget.bytes_read == 2 * fetch.MAX_BYTES
    with pytest.raises(fetch.FetchError, match='byte budget'):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert len(calls) == 2


def test_partial_stream_stops_at_remaining_aggregate_budget(transport):
    calls, replies = transport
    class ObservedResponse(Response):
        consumed = 0
        def read(self, size):
            chunk = super().read(size)
            self.consumed += len(chunk)
            return chunk
    response = ObservedResponse(b'x' * 2048)
    replies.append(response)
    budget = fetch.FetchBudget(max_bytes=1024)
    with pytest.raises(fetch.FetchError, match='byte budget'):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert budget.bytes_read == response.consumed == 1024
    assert len(calls) == 1 and calls[0]['closed']


@pytest.mark.parametrize('headers,body', [
    ({'Content-Encoding': 'gzip'}, b'invalid gzip'),
    ({'Content-Length': '50'}, b'truncated'),
    ({}, b'x' * (fetch.MAX_BYTES + 1)),
], ids=['invalid-gzip', 'truncated', 'oversized'])
def test_failed_decoding_truncation_and_oversize_keep_consumed_byte_charge(transport, headers, body):
    _, replies = transport
    replies.append(Response(body, headers=headers))
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert 0 < budget.bytes_read <= len(body)


@pytest.mark.parametrize('chunks,consumed', [
    (b'10\r\n12345678', 8),
    (b'8\r\n12345678\r\n10\r\nabcdefgh', 16),
], ids=['first-truncated-chunk', 'nested-partial-omitted-by-stdlib'])
def test_real_http_response_failed_chunk_reads_remain_charged(transport, chunks, consumed):
    calls, replies = transport
    class Socket:
        def makefile(self, *_args):
            return io.BytesIO(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                              b'Content-Type: text/plain\r\n\r\n' + chunks)
    response = http.client.HTTPResponse(Socket())
    response.begin()
    replies.append(response)
    budget = fetch.FetchBudget(max_bytes=64)
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert consumed <= budget.bytes_read <= 64
    with pytest.raises(fetch.FetchError, match='byte budget'):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert len(calls) == 1


def test_read_failure_without_a_partial_receipt_retains_conservative_charge(transport):
    _, replies = transport
    class FailedRead(Response):
        def read(self, size):
            self.stream.read(size // 2)
            raise ConnectionResetError('Synthetic private content must not enter errors')
    replies.append(FailedRead(b'x' * 64))
    budget = fetch.FetchBudget(max_bytes=64)
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/role', ['example.com'], budget)
    assert budget.bytes_read == 64


@pytest.mark.parametrize('framing', [
    b'Transfer-Encoding: chunked\r\nContent-Length: 65536\r\n',
    b'Transfer-Encoding: chunked\r\nContent-Length: 0\r\n',
    b'Transfer-Encoding: gzip\r\nContent-Length: 65536\r\n',
    b'Transfer-Encoding: gzip\r\n',
    b'Transfer-Encoding: gzip, chunked\r\n',
    b'Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n',
    b'Transfer-Encoding: chunked \r\n',
    b'Transfer-Encoding: chunked\t\r\n',
], ids=['chunked-with-length', 'chunked-with-zero-length', 'unsupported-with-length',
        'unsupported-transfer', 'multiple-transfers', 'duplicate-transfer',
        'parser-disagrees-trailing-space', 'parser-disagrees-trailing-tab'])
def test_real_http_response_rejects_ambiguous_framing_before_body_read(transport, framing):
    prefix = b'Company: Synthetic\n' + b'\n' * (65536 - len(b'Company: Synthetic\n'))
    tail = b'CAPTCHA: access restricted\n'
    wire = (b'HTTP/1.1 200 OK\r\n' + framing + b'Content-Type: text/plain\r\n\r\n'
            + b'10000\r\n' + prefix + b'\r\n' + f'{len(tail):x}\r\n'.encode()
            + tail + b'\r\n0\r\n\r\n')
    class Socket:
        def makefile(self, *_args):
            return io.BytesIO(wire)
    response = http.client.HTTPResponse(Socket())
    response.begin()
    reads = []
    original_read = response.read
    def observed_read(size):
        reads.append(size)
        return original_read(size)
    response.read = observed_read
    transport[1].append(response)
    with pytest.raises(fetch.FetchError, match='framing'):
        fetch.acquire('https://example.com/role', ['example.com'], fetch.FetchBudget())
    assert not reads


@pytest.mark.parametrize('length', [b'5\r\nContent-Length: 6', b'5, 5', b'+5', b'1_0', b'-0', b''])
def test_real_http_response_rejects_malformed_or_conflicting_length_before_read(transport, length):
    class Socket:
        def makefile(self, *_args):
            return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: '
                              + length + b'\r\n\r\nhello')
    response = http.client.HTTPResponse(Socket())
    response.begin()
    reads = []
    original_read = response.read
    def observed_read(size):
        reads.append(size)
        return original_read(size)
    response.read = observed_read
    transport[1].append(response)
    with pytest.raises(fetch.FetchError, match='length'):
        fetch.acquire('https://example.com/role', ['example.com'], fetch.FetchBudget())
    assert not reads


@pytest.mark.parametrize('url', [
    'http://example.com/role', 'https://example.com:444/',
    'https://user@example.com/', 'https://user:pass@example.com/',
    'https://example.com.evil.test/', 'https://sub.example.com/',
    'https://example.com./', 'https://example.com\\@evil.test/',
    'https://example.com/\nheader', 'https://%65xample.com/',
    'https://127.0.0.1/', 'https://[::1]/', 'https://169.254.169.254/',
    'https://[::ffff:127.0.0.1]/', 'https://2130706433/',
])
def test_hostile_url_never_connects(transport, url):
    calls, _ = transport
    with pytest.raises(fetch.FetchError):
        fetch.acquire(url, ['example.com'], fetch.FetchBudget())
    assert not calls


@pytest.mark.parametrize('host', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
                                  '::1', 'fe80::1', 'fc00::1', '::ffff:8.8.8.8',
                                  '2002:0808:0808::1', '192.0.2.1', '100.64.0.1',
                                  '224.0.0.1', 'ff02::1', '64:ff9b::a00:1'])
def test_confirming_private_or_transition_host_cannot_override_policy(transport, host):
    url_host = f'[{host}]' if ':' in host else host
    with pytest.raises(fetch.FetchError):
        fetch.acquire(f'https://{url_host}/', [host], fetch.FetchBudget())
    assert not transport[0]


def test_idna_exact_match_capsule_and_no_secret_headers(transport):
    calls, replies = transport
    body = '<html><head><title>ignore</title></head><body><h1>Rôle</h1><script>bad()</script><p>Team\r\nimpact</p></body></html>'.encode()
    replies.append(Response(body, headers={'Content-Type': 'text/html; charset=utf-8'}))
    result = fetch.acquire('https://bücher.example/job?q=1#fragment',
                           ['bücher.example'], fetch.FetchBudget())
    assert result.final_url == 'https://xn--bcher-kva.example/job?q=1'
    assert result.canonical_text == 'Rôle\nTeam\nimpact'
    assert result.original_bytes == body
    assert result.original_sha256 == hashlib.sha256(body).hexdigest()
    assert result.canonical_sha256 == hashlib.sha256(result.canonical_text.encode()).hexdigest()
    assert result.provenance == 'anonymous_static_download'
    assert result.completeness == 'unassessed'
    assert result.acquired_at.endswith('+00:00')
    assert calls[0]['host'] == 'xn--bcher-kva.example'
    assert calls[0]['method'] == 'GET'
    assert not {'Authorization', 'Cookie', 'Proxy-Authorization'} & calls[0]['headers'].keys()
    assert calls[0]['closed']


def test_dns_mixed_private_answers_denied_before_connection(transport, monkeypatch):
    monkeypatch.setattr(fetch, '_resolve', lambda *args: ['93.184.216.34', '127.0.0.1'])
    with pytest.raises(fetch.FetchError, match='Non-public'):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert not transport[0]


def test_redirect_revalidates_dns_and_consumes_budget(transport, monkeypatch):
    calls, replies = transport
    replies.append(Response(status=302, headers={'Location': '/new'}))
    addresses = iter([['93.184.216.34'], ['127.0.0.1']])
    monkeypatch.setattr(fetch, '_resolve', lambda *args: next(addresses))
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/', ['example.com'], budget)
    assert len(calls) == 1 and calls[0]['closed']
    assert budget.requests == 2


@pytest.mark.parametrize('location', ['http://example.com/new', 'https://evil.test/',
                                      'https://user@example.com/', 'https://127.0.0.1/'])
def test_redirect_cannot_expand_policy(transport, location):
    transport[1].append(Response(status=302, headers={'Location': location}))
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert len(transport[0]) == 1


def test_redirect_loop_bounded(transport):
    transport[1].extend(Response(status=302, headers={'Location': '/'}) for _ in range(4))
    with pytest.raises(fetch.FetchError, match='Redirect limit'):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert len(transport[0]) == 4


def test_budget_exhaustion_and_run_expiry_never_connect(transport):
    for budget in (fetch.FetchBudget(requests=10),
                   fetch.FetchBudget(started=time.monotonic() - 121),
                   fetch.FetchBudget(max_requests=11)):
        with pytest.raises(fetch.FetchError):
            fetch.acquire('https://example.com/', ['example.com'], budget)
    assert not transport[0]


@pytest.mark.parametrize('body,headers', [
    (b'x' * (fetch.MAX_BYTES + 1), {}),
    (b'x', {'Content-Length': str(fetch.MAX_BYTES + 1)}),
    (b'x', {'Content-Length': 'nonsense'}),
    (b'x', {'Content-Length': '2'}),
    (gzip.compress(b'x' * (fetch.MAX_BYTES + 1), mtime=0), {'Content-Encoding': 'gzip'}),
    (gzip.compress(b'x', mtime=0)[:-2], {'Content-Encoding': 'gzip'}),
    (gzip.compress(b'x', mtime=0) + gzip.compress(b'y', mtime=0), {'Content-Encoding': 'gzip'}),
    (b'bad', {'Content-Encoding': 'deflate'}),
    (b'bad', {'Content-Encoding': 'br'}),
])
def test_body_limits_compression_truncation_and_lengths(transport, body, headers):
    transport[1].append(Response(body, headers=headers))
    with pytest.raises(fetch.FetchError):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert transport[0][0]['closed']


def test_compressed_original_and_decoded_text_preserved(transport):
    original = gzip.compress(b'Public facts', mtime=0)
    transport[1].append(Response(original, headers={'Content-Encoding': 'gzip'}))
    result = fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert result.original_bytes == original and result.canonical_text == 'Public facts'
    assert result.content_encoding == 'gzip'


def test_dns_subprocess_deadline_not_a_leaking_thread(monkeypatch):
    def timeout(command, **kwargs):
        assert command[1] == '-I'
        assert command[-1] == 'example.com'
        assert 0 < kwargs['timeout'] <= 1
        assert kwargs['stderr'] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(fetch.subprocess, 'run', timeout)
    with pytest.raises(fetch.FetchError, match='DNS'):
        fetch._resolve('example.com', time.monotonic() + 1)


def test_dns_all_answers_validated(monkeypatch):
    monkeypatch.setattr(fetch.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0,
                                                    json.dumps(['8.8.8.8', '::1']).encode()))
    with pytest.raises(fetch.FetchError):
        fetch._resolve('example.com', time.monotonic() + 1)


def test_tls_pinned_numeric_connection_and_original_hostname(monkeypatch):
    events = []

    class Socket:
        def settimeout(self, timeout):
            assert 0 < timeout <= 2

        def connect(self, target):
            events.append(('connect', target))

        def close(self):
            events.append(('close',))

    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            events.append(('tls', server_hostname))
            raise ssl.SSLCertVerificationError('synthetic mismatch')

    monkeypatch.setattr(fetch.socket, 'socket', lambda *args: Socket())
    monkeypatch.setattr(fetch, '_tls_context', lambda: Context())
    monkeypatch.setattr(fetch.socket, 'getaddrinfo', lambda *args: pytest.fail('second DNS'))
    connection = fetch._PinnedHTTPS('example.com', '93.184.216.34', time.monotonic() + 2)
    with pytest.raises(ssl.SSLCertVerificationError):
        connection.connect()
    assert events == [('connect', ('93.184.216.34', 443)), ('tls', 'example.com'), ('close',)]


def test_absolute_deadline_each_header_body_recv(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: now[0])

    class Socket:
        def settimeout(self, timeout):
            assert timeout == 5

        def recv_into(self, buffer):
            buffer[0] = 65
            return 1

    reader = fetch._DeadlineReader(Socket(), 15)
    assert reader.readinto(bytearray(1)) == 1
    now[0] = 15
    with pytest.raises(fetch.FetchError, match='deadline'):
        reader.readinto(bytearray(1))


def test_real_chunked_http_parser_and_deadline_reader():
    wire = (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
            b'Content-Type: text/plain\r\n\r\n4\r\nRole\r\n0\r\n\r\n')

    class Socket:
        def __init__(self):
            self.stream = io.BytesIO(wire)

        def settimeout(self, timeout):
            assert timeout > 0

        def recv_into(self, buffer):
            return self.stream.readinto(buffer)

    response = fetch.http.client.HTTPResponse(
        fetch._PinnedSocket(Socket(), time.monotonic() + 2))
    response.begin()
    raw, decoded, encoding = fetch._body(response, time.monotonic() + 2)
    assert raw == decoded == b'Role' and encoding == 'identity'


def test_owned_loopback_receiver_observes_zero_requests(monkeypatch):
    """Real listener is production-denied; no private-host override is installed."""
    receiver = socket.socket()
    try:
        try:
            receiver.bind(('127.0.0.1', 443))
        except OSError:
            pytest.skip('Owned loopback port 443 unavailable; receiver proof not run')
        receiver.listen()
        receiver.settimeout(.05)
        monkeypatch.setattr(fetch, '_resolve', lambda *args: ['127.0.0.1'])
        with pytest.raises(fetch.FetchError):
            fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
        with pytest.raises(TimeoutError):
            receiver.accept()
    finally:
        receiver.close()


@pytest.mark.parametrize('content,media', [
    (b'<script>render role here</script>', 'text/html'),
    (b'\xff', 'text/plain'), (b'\x00', 'text/plain'),
    (b'<html>bad</html>', 'application/octet-stream'),
    (b'not a PDF', 'application/pdf'),
])
def test_unsupported_static_content_fails_closed(content, media):
    with pytest.raises(ExtractionError):
        extract(content, media, timeout=1)


def test_pdf_uses_bounded_existing_subprocess(monkeypatch):
    def run(command, **kwargs):
        assert command[1] == '-I' and command[2].endswith('_parse_pdf.py')
        assert kwargs['timeout'] == 2
        assert kwargs['stderr'] == subprocess.DEVNULL
        return subprocess.CompletedProcess(command, 0, b'["Role page", "Requirements"]')
    monkeypatch.setattr('copilot.research.extract.subprocess.run', run)
    assert extract(b'%PDF-synthetic', 'application/pdf', timeout=2) == (
        'Role page\n\nRequirements', 'pypdf-6.19.0-page-text-v1')


def test_default_tls_context_verifies_and_never_writes_keylog(tmp_path, monkeypatch):
    keylog = tmp_path / 'must-not-exist.log'
    monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    context = fetch._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.keylog_filename is None
    assert not keylog.exists()


def test_slow_body_cannot_return_success_after_deadline(transport, monkeypatch):
    real_clock = time.monotonic
    expired = [False]

    class SlowResponse(Response):
        def read(self, size):
            expired[0] = True
            return super().read(size)

    transport[1].append(SlowResponse())
    monkeypatch.setattr(fetch.time, 'monotonic',
                        lambda: real_clock() + (20 if expired[0] else 0))
    with pytest.raises(fetch.FetchError, match='deadline'):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert transport[0][0]['closed']


def test_real_http_connection_close_retains_body_then_closes_socket():
    # Production getresponse closes its socket before returning a close-delimited body.
    # Exercise stdlib itself, not only the injected Response stub.
    wire = (b'HTTP/1.1 200 OK\r\nConnection: close\r\n'
            b'Content-Type: text/plain\r\n\r\nPublic role')

    class Socket:
        def __init__(self):
            self.stream, self.closed = io.BytesIO(wire), False

        def settimeout(self, timeout):
            assert not self.closed

        def recv_into(self, buffer):
            assert not self.closed
            return self.stream.readinto(buffer)

        def close(self):
            self.closed = True

    sock = Socket()
    connection = fetch.http.client.HTTPConnection('example.com')
    connection.sock = fetch._PinnedSocket(sock, time.monotonic() + 2)
    connection._HTTPConnection__state = fetch.http.client._CS_REQ_SENT
    connection._method = 'GET'
    response = connection.getresponse()
    assert not sock.closed
    assert fetch._body(response, time.monotonic() + 2)[0] == b'Public role'
    assert sock.closed
    response.close()
    connection.close()


def test_owned_unprivileged_receiver_observes_denial_for_its_exact_url():
    """Actual requested destination rejects non-443 before any outbound socket."""
    receiver = socket.socket()
    try:
        receiver.bind(('127.0.0.1', 0))
        receiver.listen()
        receiver.settimeout(.05)
        port = receiver.getsockname()[1]
        with pytest.raises(fetch.FetchError):
            fetch.acquire(f'https://127.0.0.1:{port}/', ['127.0.0.1'], fetch.FetchBudget())
        with pytest.raises(TimeoutError):
            receiver.accept()
    finally:
        receiver.close()


def test_real_socket_read_times_out_on_remaining_absolute_budget():
    left, right = socket.socketpair()
    try:
        started = time.monotonic()
        reader = fetch._DeadlineReader(left, started + .03)
        with pytest.raises(TimeoutError):
            reader.readinto(bytearray(1))
        assert time.monotonic() - started < .5
    finally:
        left.close()
        right.close()


def test_review_reproduction_html_nesting_is_rejected_in_isolated_process(transport):
    content = b'<template>' * 100_000 + b'</a>' * 100_000 + b'Visible'
    assert len(content) == 1_400_007 < fetch.MAX_BYTES
    transport[1].append(Response(content, headers={'Content-Type': 'text/html'}))
    started = time.monotonic()
    with pytest.raises(fetch.FetchError, match='Static HTML extraction'):
        fetch.acquire('https://example.com/', ['example.com'], fetch.FetchBudget())
    assert time.monotonic() - started < 2
    assert transport[0][0]['closed']


def test_real_html_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    import copilot.research.extract as extraction

    helper = tmp_path / 'blocked_html_parser.py'
    helper.write_text('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(extraction, 'HTML_HELPER', helper)
    original_popen = subprocess.Popen
    children = []

    def tracked_popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(extraction.subprocess, 'Popen', tracked_popen)
    started = time.monotonic()
    with pytest.raises(ExtractionError, match='Static HTML extraction'):
        extract(b'<p>Public role</p>', 'text/html', timeout=.08)
    assert time.monotonic() - started < 2
    assert len(children) == 1
    assert children[0].returncode is not None  # subprocess.run waited/reaped after kill.
    assert children[0].poll() is not None


def test_acquire_passes_remaining_run_deadline_to_real_html_child(transport, tmp_path, monkeypatch):
    import copilot.research.extract as extraction

    helper = tmp_path / 'blocked_html_parser.py'
    helper.write_text('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(extraction, 'HTML_HELPER', helper)
    transport[1].append(Response(b'<p>Role</p>', headers={'Content-Type': 'text/html'}))
    # The run budget is deliberately smaller than the 15-second per-fetch maximum.
    started = time.monotonic()
    with pytest.raises(fetch.FetchError, match='Static HTML extraction'):
        fetch.acquire('https://example.com/', ['example.com'],
                      fetch.FetchBudget(run_seconds=.1))
    assert time.monotonic() - started < 2
    assert transport[0][0]['closed']


def test_html_resource_limits_and_work_caps_are_installed_before_parsing(tmp_path):
    # Inspect the actual child resource limits before loading untrusted input by using
    # runpy with a non-main name; the helper never opens application data or a network.
    import copilot.research.extract as extraction

    command = [
        fetch.sys.executable, '-I', '-c',
        'import json,resource,runpy,sys; m=runpy.run_path(sys.argv[1],run_name="limits"); '
        'print(json.dumps([resource.getrlimit(resource.RLIMIT_AS),'
        'resource.getrlimit(resource.RLIMIT_CPU),resource.getrlimit(resource.RLIMIT_NOFILE),'
        'm["MAX_DEPTH"],m["MAX_EVENTS"]]))', str(extraction.HTML_HELPER),
    ]
    result = subprocess.run(command, capture_output=True, timeout=2, check=True)
    assert json.loads(result.stdout) == [
        [128 * 1024 * 1024] * 2, [2, 2], [32, 32], 512, 200_000,
    ]


def test_html_isolation_preserves_canonical_text_and_new_version():
    text, version = extract(
        '<head><title>Hidden</title></head><p>Rôle &amp; team</p><script>secret</script>'.encode(),
        'text/html', timeout=2,
    )
    assert text == 'Rôle & team'
    assert version == 'stdlib-html-isolated-text-v2'


def test_html_parser_rejects_excessive_output_and_non_linux(monkeypatch):
    with pytest.raises(ExtractionError):
        extract(b'a' * 1_000_001, 'text/html', timeout=2)
    import copilot.research.extract as extraction
    monkeypatch.setattr(extraction.sys, 'platform', 'win32')
    with pytest.raises(ExtractionError, match='requires Linux'):
        extract(b'<p>Role</p>', 'text/html', timeout=2)


# Fixed Greenhouse acquisition is a separate, unused transport lane. Real
# HTTPResponse headers preserve duplicates, unlike a dict-backed response stub.
GH_API = 'https://boards-api.greenhouse.io/v1/boards/synthetic_board/jobs/12345'
GH_HOSTS = ['boards-api.greenhouse.io']


def gh_response(body=b'{}', *, status=200, headers=None, framing=True):
    class Socket:
        def makefile(self, mode):
            return io.BytesIO(wire)
    pairs = [('Content-Type', 'application/json')] if headers is None else list(headers)
    if framing:
        pairs.append(('Content-Length', str(len(body))))
    wire = (f'HTTP/1.1 {status} Synthetic\r\n'.encode()
            + b''.join(f'{key}: {value}\r\n'.encode() for key, value in pairs)
            + b'\r\n' + body)
    response = http.client.HTTPResponse(Socket())
    response.begin()
    return response


def gh_acquire(budget=None, *, url=GH_API, hosts=None):
    # Attribute lookup intentionally occurs at test execution, not collection.
    return fetch.acquire_greenhouse_json(
        url, GH_HOSTS if hosts is None else hosts, budget or fetch.FetchBudget())


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
def test_fixed_json_preserves_raw_and_decoded_representations(transport, encoding):
    import zlib
    decoded = '{"literal":"Café 🚀 <p>&amp; no experience</p>"}'.encode()
    raw = gzip.compress(decoded, mtime=0) if encoding == 'gzip' else (
        zlib.compress(decoded) if encoding == 'deflate' else decoded)
    transport[1].append(gh_response(raw, headers=[
        ('Content-Type', 'application/json'), ('Content-Encoding', encoding)]))
    budget = fetch.FetchBudget(max_bytes=8 * 1024 * 1024)
    result = gh_acquire(budget)
    assert result.original_bytes == raw
    assert result.decoded_json_bytes == decoded
    assert result.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.decoded_json_sha256 == hashlib.sha256(decoded).hexdigest()
    assert result.requested_url == result.final_url == GH_API
    assert result.media_type == 'application/json'
    assert result.content_encoding == encoding
    assert budget.requests == 1 and budget.bytes_read == len(raw)
    call = transport[0][0]
    assert call['host'] == GH_HOSTS[0] and call['address'] == '93.184.216.34'
    assert call['method'] == 'GET'
    assert call['target'] == '/v1/boards/synthetic_board/jobs/12345'
    assert call['headers'] == {
        'Host': GH_HOSTS[0], 'User-Agent': 'ApplicationCopilot-StaticResearch/1',
        'Accept': 'application/json', 'Accept-Encoding': 'gzip,deflate', 'Connection': 'close'}
    assert call['closed']


@pytest.mark.parametrize('suffix', ['?questions=true', '?', '#', '#x', '/', '/other'])
def test_fixed_json_rejects_endpoint_variations_before_reservation(transport, suffix):
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget, url=GH_API + suffix)
    assert budget.requests == budget.bytes_read == 0 and transport[0] == []


@pytest.mark.parametrize('url', [
    GH_API.replace('https:', 'http:'), GH_API.replace('boards-api.', 'evil.boards-api.'),
    GH_API.replace('io/', 'io:443/'), GH_API.replace('io/', 'io:444/'),
    GH_API.replace('https://', 'https://user@'), GH_API.replace('boards-api', 'BOARDS-API'),
    GH_API.replace('12345', '012345'), GH_API.replace('12345', '%312345'),
    GH_API.replace('12345', '0'), GH_API.replace('12345', str(2**63)),
])
def test_fixed_json_never_normalizes_forbidden_endpoint_spelling(transport, url):
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget, url=url)
    assert budget.requests == 0 and transport[0] == []


@pytest.mark.parametrize('hosts', [[], ['example.com'], GH_HOSTS * 21,
    GH_HOSTS[0], (GH_HOSTS[0],), [123], ['x' * 254], ['é.example'], [None]])
def test_fixed_json_raw_host_list_is_bounded_before_normalization(transport, monkeypatch, hosts):
    normalized = []
    original = fetch._host
    monkeypatch.setattr(fetch, '_host', lambda host: (normalized.append(host), original(host))[1])
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget, hosts=hosts)
    assert budget.requests == 0 and transport[0] == []
    if hosts != ['example.com']:
        assert normalized == []


def test_fixed_json_rejects_host_generator_without_consuming_it(transport):
    consumed = []
    def hosts():
        consumed.append(True)
        yield GH_HOSTS[0]
    with pytest.raises(fetch.FetchError):
        gh_acquire(hosts=hosts())
    assert consumed == [] and transport[0] == []


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 201, 204, 404, 410, 429, 500])
def test_fixed_json_never_follows_redirects_retries_or_infers_closed(transport, status):
    transport[1].append(gh_response(status=status, headers=[
        ('Content-Type', 'application/json'), ('Location', GH_API)]))
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 1 and budget.bytes_read == 0
    assert len(transport[0]) == 1 and transport[0][0]['closed']


@pytest.mark.parametrize('media', [
    'application/json', ' Application/JSON ', 'application/json; charset=utf-8',
    'application/json; CHARSET="UTF-8"', 'application/json ; charset = utf-8',
])
def test_fixed_json_accepts_only_documented_local_charset_subset(transport, media):
    transport[1].append(gh_response(headers=[('Content-Type', media)]))
    assert gh_acquire().media_type == 'application/json'


@pytest.mark.parametrize('headers', [
    [], [('Content-Type', 'text/html')], [('Content-Type', 'application/octet-stream')],
    [('Content-Type', 'application/problem+json')],
    [('Content-Type', 'application/json; charset=latin1')],
    [('Content-Type', 'application/json; charset=utf-8; charset=utf-8')],
    [('Content-Type', 'application/json; charset=utf-8; other=x')],
    [('Content-Type', 'application/json;')],
    [('Content-Type', 'application/json'), ('Content-Type', 'application/json')],
    [('Content-Type', 'application/json'), ('Content-Type', 'text/html')],
])
def test_fixed_json_rejects_missing_duplicate_and_ambiguous_real_http_headers(transport, headers):
    transport[1].append(gh_response(headers=headers))
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 1 and budget.bytes_read == 0 and transport[0][0]['closed']


@pytest.mark.parametrize('size', [0, 256 * 1024 + 1, fetch.MAX_BYTES, fetch.MAX_BYTES + 1])
def test_fixed_json_rejects_empty_or_over_parser_bound_payload(transport, size):
    transport[1].append(gh_response(b'x' * size))
    with pytest.raises(fetch.FetchError):
        gh_acquire()
    assert transport[0][0]['closed']


def test_fixed_json_accepts_exact_parser_byte_boundary_without_parsing(transport):
    raw = b' ' * (256 * 1024)
    transport[1].append(gh_response(raw))
    assert gh_acquire().decoded_json_bytes == raw


@pytest.mark.parametrize('headers,body,framing', [
    ([('Content-Encoding', 'br')], b'{}', True),
    ([('Content-Encoding', 'gzip')], b'bad', True),
    ([('Content-Encoding', 'gzip')], gzip.compress(b'{}', mtime=0)[:-1], True),
    ([('Content-Encoding', 'gzip')], gzip.compress(b'{}', mtime=0) * 2, True),
    ([('Content-Encoding', 'gzip')], gzip.compress(b'x' * (fetch.MAX_BYTES + 1), mtime=0), True),
    ([('Content-Length', '10')], b'{}', False),
    ([('Content-Length', 'wat')], b'{}', False),
    ([('Content-Length', '2'), ('Transfer-Encoding', 'chunked')], b'0\r\n\r\n', False),
    ([('Transfer-Encoding', 'chunked')], b'4\r\n{}', False),
])
def test_fixed_json_rejects_encoding_and_framing_without_success(transport, headers, body, framing):
    transport[1].append(gh_response(body, headers=[('Content-Type', 'application/json')] + headers,
                                    framing=framing))
    with pytest.raises(fetch.FetchError):
        gh_acquire()
    assert transport[0][0]['closed']


@pytest.mark.parametrize('addresses', [[], ['127.0.0.1'], ['93.184.216.34', '10.0.0.1'],
    ['169.254.1.1'], ['::ffff:93.184.216.34'], ['2002:5db8:d822::1'], ['93.184.216.34'] * 65])
def test_fixed_json_revalidates_resolver_output_before_connect(transport, monkeypatch, addresses):
    monkeypatch.setattr(fetch, '_resolve', lambda *_: addresses)
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 1 and transport[0] == []


@pytest.mark.parametrize('delay', [15, 16])
def test_fixed_json_preflight_expiry_has_no_budget_or_dns_effects(transport, monkeypatch, delay):
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    original = fetch._host
    def delayed_host(host):
        clock[0] += delay
        return original(host)
    monkeypatch.setattr(fetch, '_host', delayed_host)
    dns = []
    monkeypatch.setattr(fetch, '_resolve', lambda *args: dns.append(args))
    budget = fetch.FetchBudget(started=100)
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 0 and dns == [] and transport[0] == []


def test_fixed_json_partial_preflight_preserves_original_deadline(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    original = fetch._host
    def delayed_host(host):
        clock[0] += 1
        return original(host)
    monkeypatch.setattr(fetch, '_host', delayed_host)
    dns = []
    monkeypatch.setattr(fetch, '_resolve', lambda host, deadline: (
        dns.append(deadline), ['93.184.216.34'])[1])
    transport[1].append(gh_response())
    gh_acquire(fetch.FetchBudget(started=100))
    assert dns == [115] and transport[0][0]['deadline'] == 115


def test_fixed_json_checks_deadline_after_hashing(transport, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    original = fetch.hashlib.sha256
    def delayed_hash(value):
        clock[0] = 115
        return original(value)
    monkeypatch.setattr(fetch.hashlib, 'sha256', delayed_hash)
    transport[1].append(gh_response())
    with pytest.raises(fetch.FetchError):
        gh_acquire(fetch.FetchBudget(started=100))
    assert transport[0][0]['closed']


def test_fixed_json_connection_closes_even_when_response_cleanup_raises(transport):
    response = gh_response()
    original_close = response.close
    def broken_close():
        original_close()
        raise OSError('synthetic cleanup failure')
    response.close = broken_close
    transport[1].append(response)
    with pytest.raises((fetch.FetchError, OSError)):
        gh_acquire()
    assert transport[0][0]['closed']


def test_fixed_json_two_acquisitions_share_budget_without_reset(transport):
    budget = fetch.FetchBudget(max_requests=2, max_bytes=8 * 1024 * 1024)
    for _ in range(2):
        transport[1].append(gh_response())
        gh_acquire(budget)
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 2 and budget.bytes_read == 4 and len(transport[0]) == 2


@pytest.mark.parametrize('cap', [True, float('nan'), float('inf'), '115', None])
def test_fixed_json_private_deadline_cap_rejects_nonfinite_or_inexact_types(transport, cap):
    budget = fetch.FetchBudget()
    with pytest.raises(fetch.FetchError):
        fetch._acquire_greenhouse_json_until(GH_API, GH_HOSTS, budget, cap)
    assert budget.requests == 0 and transport[0] == []


@pytest.mark.parametrize('kind', ['request', 'run', 'body'])
def test_fixed_json_exhausted_shared_budget_performs_no_dns_or_request(transport, monkeypatch, kind):
    budget = fetch.FetchBudget()
    if kind == 'request':
        budget.requests = budget.max_requests
    elif kind == 'body':
        budget.bytes_read = budget.max_bytes
    else:
        budget.started -= 121
    dns = []
    monkeypatch.setattr(fetch, '_resolve', lambda *args: dns.append(args))
    before = (budget.requests, budget.bytes_read)
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert (budget.requests, budget.bytes_read) == before
    assert dns == [] and transport[0] == []


@pytest.mark.parametrize('stage', ['dns', 'read'])
def test_fixed_json_expiry_during_io_denies_return_and_closes_acquired_resources(transport, monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    response = gh_response()
    if stage == 'dns':
        def resolve(*args):
            clock[0] = 115
            return ['93.184.216.34']
        monkeypatch.setattr(fetch, '_resolve', resolve)
    else:
        original = response.read
        def read(size):
            clock[0] = 115
            return original(size)
        response.read = read
    transport[1].append(response)
    with pytest.raises(fetch.FetchError):
        gh_acquire(fetch.FetchBudget(started=100))
    assert all(call['closed'] for call in transport[0])


@pytest.mark.parametrize('stage', ['request', 'response'])
def test_fixed_json_request_and_response_failures_close_connection(monkeypatch, stage):
    closed = []
    monkeypatch.setattr(fetch, '_resolve', lambda *_: ['93.184.216.34'])
    class Connection:
        def __init__(self, *args):
            pass
        def request(self, *args, **kwargs):
            if stage == 'request':
                raise OSError('synthetic request failure')
        def getresponse(self):
            raise http.client.HTTPException('synthetic response failure')
        def close(self):
            closed.append(True)
    monkeypatch.setattr(fetch, '_PinnedHTTPS', Connection)
    with pytest.raises(fetch.FetchError):
        gh_acquire()
    assert closed == [True]


def test_fixed_json_partial_exception_keeps_body_reservation(transport):
    response = gh_response(b'x' * 100)
    def broken_read(size):
        raise http.client.IncompleteRead(b'partial')
    response.read = broken_read
    transport[1].append(response)
    budget = fetch.FetchBudget(max_bytes=1024)
    with pytest.raises(fetch.FetchError):
        gh_acquire(budget)
    assert budget.requests == 1 and budget.bytes_read == 1024
    assert transport[0][0]['closed']


def _assert_fixed_json_denies_cleanup_expiry(transport, monkeypatch, expiry_stage):
    clock = [100.0]
    closed = []
    monkeypatch.setattr(fetch.time, 'monotonic', lambda: clock[0])
    response = gh_response()
    original_response_close = response.close

    def close_response():
        original_response_close()
        closed.append('response')
        if expiry_stage == 'response':
            clock[0] = 115.0

    response.close = close_response
    original_connection = fetch._PinnedHTTPS

    class Connection(original_connection):
        def close(self):
            super().close()
            closed.append('connection')
            if expiry_stage == 'connection':
                clock[0] = 115.0

    monkeypatch.setattr(fetch, '_PinnedHTTPS', Connection)
    transport[1].append(response)
    budget = fetch.FetchBudget(started=100.0)
    try:
        with pytest.raises(fetch.FetchError, match='deadline'):
            gh_acquire(budget)
    finally:
        # A late denial does not erase the completed request/body charge, and
        # both independent resources must close before the return boundary.
        assert closed == ['response', 'connection']
        assert clock[0] == 115.0
        assert budget.requests == 1 and budget.bytes_read == 2
        assert transport[0][0]['deadline'] == 115.0
        assert transport[0][0]['closed'] and response.closed


def test_fixed_json_response_close_exact_deadline_expiry_denies_return(transport, monkeypatch):
    _assert_fixed_json_denies_cleanup_expiry(transport, monkeypatch, 'response')


def test_fixed_json_connection_close_exact_deadline_expiry_denies_return(transport, monkeypatch):
    _assert_fixed_json_denies_cleanup_expiry(transport, monkeypatch, 'connection')


@pytest.mark.parametrize('test_function,body_index', [
    (test_body_limits_compression_truncation_and_lengths, 0),
    (test_fixed_json_rejects_encoding_and_framing_without_success, 1),
])
def test_import_time_gzip_fixtures_have_fixed_mtime(test_function, body_index):
    """Fixture identities must not depend on when pytest imports this module."""
    marker = next(mark for mark in test_function.pytestmark if mark.name == 'parametrize')
    timestamps = []
    for parameters in marker.args[1]:
        body = parameters[body_index]
        if not body.startswith(b'\x1f\x8b\x08'):
            continue
        # Include the second member of intentionally concatenated gzip fixtures.
        offset = 0
        while (offset := body.find(b'\x1f\x8b\x08', offset)) != -1:
            timestamps.append(int.from_bytes(body[offset + 4:offset + 8], 'little'))
            offset += 10
    assert timestamps == [0, 0, 0, 0]
