"""Actual stdlib HTTP parser/socket deadline checks without provider TLS or keys."""
import http.client
import socket
import subprocess
import threading
import time

import pytest

from copilot import provider_http as transport
from copilot.provider import DefinitelyUnsent, ResponsesHTTP


def parser_fixture(chunks, delay, seconds):
    client, server = socket.socketpair()
    deadline = time.monotonic() + seconds
    connection = http.client.HTTPConnection('synthetic.test')
    connection.sock = transport.DeadlineSocket(client, deadline)
    connection.request('GET', '/')
    server.recv(65536)
    def write():
        try:
            for chunk in chunks:
                time.sleep(delay)
                server.sendall(chunk)
        except OSError:
            pass
        finally:
            server.close()
    thread = threading.Thread(target=write)
    thread.start()
    return connection, thread


def test_slow_header_parser_respects_total_deadline():
    conn, producer = parser_fixture([b'HTTP/1.1 200 OK\r\n', b'X-A: a\r\n'] * 8, 0.025, 0.08)
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, OSError)):
            conn.getresponse()
        assert time.monotonic() - started < 0.3
    finally:
        conn.close()
        producer.join(timeout=1)


def test_close_delimited_body_keeps_deadline_after_connection_detaches():
    conn, producer = parser_fixture([b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n', *([b'x'] * 8)], 0.025, 0.09)
    response = conn.getresponse()
    assert conn.sock is None
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, OSError)):
            while response.read1(1):
                pass
        assert time.monotonic() - started < 0.3
    finally:
        response.close()
        conn.close()
        producer.join(timeout=1)


def test_late_connect_does_not_invoke_guard_or_request(monkeypatch):
    calls = []
    class LateConnection:
        def __init__(self, deadline):
            self.deadline = deadline
        def connect(self):
            monkeypatch.setattr(transport.time, 'monotonic', lambda: self.deadline + 1)
        def request(self, *_args, **_kwargs):
            calls.append('request')
        def close(self):
            pass
    monkeypatch.setattr('copilot.provider.ProviderHTTPS', LateConnection)
    with pytest.raises(DefinitelyUnsent):
        ResponsesHTTP()({}, lambda: calls.append('guard'))
    assert calls == []


def test_dns_subprocess_timeout_is_bounded_and_child_reaped(monkeypatch):
    original = subprocess.run
    children = []
    original_popen = subprocess.Popen
    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child
    def blocked_dns(args, **kwargs):
        return original([args[0], '-I', '-c', 'import time; time.sleep(10)'], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(transport.subprocess, 'run', blocked_dns)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        transport.resolve(started + 0.05)
    assert time.monotonic() - started < 0.5
    assert children and all(child.poll() is not None for child in children)


def test_tls_context_does_not_honor_keylog_environment(tmp_path, monkeypatch):
    keylog = tmp_path / 'must-not-exist.keys'
    monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    context = transport.tls_context()
    assert context.check_hostname and context.verify_mode == __import__('ssl').CERT_REQUIRED
    assert context.keylog_filename is None and not keylog.exists()


def test_deadline_write_loop_cannot_reset_timeout_per_partial_write():
    class PartialSocket:
        def settimeout(self, timeout):
            self.timeout = timeout
        def send(self, data):
            time.sleep(min(self.timeout, 0.02))
            return 1
    wrapped = transport.DeadlineSocket(PartialSocket(), time.monotonic() + 0.045)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        wrapped.sendall(b'long-partial-request')
    assert time.monotonic() - started < 0.2


def test_successful_tls_wrap_then_expired_deadline_closes_tls_socket(monkeypatch):
    closed = []
    class Raw:
        def settimeout(self, _value):
            pass
        def connect(self, _address):
            pass
        def close(self):
            closed.append('raw')
    class TLS:
        def close(self):
            closed.append('tls')
    class Context:
        def wrap_socket(self, _raw, server_hostname):
            assert server_hostname == transport.HOST
            monkeypatch.setattr(transport.time, 'monotonic', lambda: 101)
            return TLS()
    monkeypatch.setattr(transport.time, 'monotonic', lambda: 0)
    monkeypatch.setattr(transport, 'resolve', lambda _deadline: '8.8.8.8')
    monkeypatch.setattr(transport.socket, 'socket', lambda *_args: Raw())
    connection = transport.ProviderHTTPS(100)
    connection._context = Context()
    with pytest.raises(TimeoutError):
        connection.connect()
    assert 'tls' in closed
    assert connection.sock is None
