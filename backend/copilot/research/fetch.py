"""Anonymous GETs with exact host policy and pinned, certificate-verified HTTPS.

No cookies, proxies, scripts, authentication or second DNS lookup after validation.
Each socket read has the remaining absolute deadline, including HTTP header reads.
DNS runs in a killable subprocess, not an unbounded executor thread. Production DNS
and TLS/IO must additionally pass environment-specific receiver-observed qualification.
"""
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from .extract import ExtractionError, extract

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3


class FetchError(ValueError):
    pass


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError('Acquisition deadline expired')
    return remaining


def _host(value):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        raise FetchError('Invalid confirmed host')
    if any(c in value for c in '/\\@%?#') or value.endswith('.'):
        raise FetchError('Invalid confirmed host')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            host = value.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise FetchError('Invalid confirmed host') from None
        if (len(host) > 253 or '.' not in host or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in host.split('.')
        )):
            raise FetchError('Invalid confirmed host')
        return host


def _public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise FetchError('Invalid resolved address') from None
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped)):
        raise FetchError('Non-public address denied')
    # Transition mechanisms can embed a destination not represented by is_global.
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.sixtofour or ip.teredo or ip in ipaddress.ip_network('64:ff9b::/96')
        or ip in ipaddress.ip_network('64:ff9b:1::/48')
    ):
        raise FetchError('IPv6 transition address denied')
    return str(ip)


def validate_url(url, allowed_hosts):
    if (not isinstance(url, str) or len(url) > 4096 or '\\' in url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise FetchError('Invalid public URL')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError()
        host = _host(parsed.hostname)
    except ValueError:
        raise FetchError('Only anonymous HTTPS port 443 URLs are allowed') from None
    if host not in allowed_hosts:
        raise FetchError('Host is not explicitly confirmed')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _public_ip(host)
    authority = f'[{host}]' if ':' in host else host
    path = parsed.path or '/'
    return urlunsplit(('https', authority, path, parsed.query, '')), host, path + (
        '?' + parsed.query if parsed.query else '')


def _resolve(host, deadline):
    """Subprocess termination bounds libc getaddrinfo; no private-host fallback."""
    try:
        return [_public_ip(host)]
    except FetchError:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise
    script = ('import json,socket,sys; '
              'a=socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM); '
              'print(json.dumps(list(dict.fromkeys(x[4][0] for x in a))[:65]))')
    try:
        result = subprocess.run([sys.executable, '-I', '-c', script, host],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=_remaining(deadline), check=False)
        addresses = json.loads(result.stdout) if result.returncode == 0 else None
        if (not isinstance(addresses, list) or not 1 <= len(addresses) <= 64
                or any(not isinstance(address, str) for address in addresses)):
            raise ValueError()
        return list(dict.fromkeys(_public_ip(address) for address in addresses))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise FetchError('DNS resolution denied or exceeded deadline') from None


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline, release=lambda: None):
        self.sock, self.deadline, self.release = sock, deadline, release

    def close(self):
        if not self.closed:
            super().close()
            self.release()

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.recv_into(buffer)


class _PinnedSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.readers = 0
        self.closed = False

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        self.sock.sendall(data)

    def makefile(self, mode):
        if mode != 'rb':
            raise FetchError('Unsupported socket stream')
        self.readers += 1
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline, self._release))

    def _release(self):
        self.readers -= 1
        if self.closed and not self.readers:
            self.sock.close()

    def close(self):
        # HTTPConnection closes a Connection:close socket BEFORE returning its body.
        # Like socket.makefile(), retain the transport until the response reader closes.
        if not self.closed:
            self.closed = True
            if not self.readers:
                self.sock.close()


def _tls_context():
    # Unlike create_default_context(), do not honor SSLKEYLOGFILE or write TLS secrets.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return context


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, deadline):
        # Default context verifies certificate chain and original DNS hostname.
        super().__init__(host, port=443, timeout=_remaining(deadline),
                         context=_tls_context())
        self.address, self.deadline = address, deadline

    def connect(self):
        address = ipaddress.ip_address(self.address)
        raw = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                            socket.SOCK_STREAM)
        try:
            raw.settimeout(_remaining(self.deadline))
            raw.connect((str(address), 443))  # Numeric socket connect; no getaddrinfo.
            raw.settimeout(_remaining(self.deadline))
            tls = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock = _PinnedSocket(tls, self.deadline)
        except BaseException:
            raw.close()
            raise


@dataclass
class FetchBudget:
    """Caller-owned budget; body-byte charges include failed/partial acquisitions.

    This is not accounting for TLS records, HTTP headers/framing or socket buffers,
    nor a durable concurrency/job authority. Reserve each bounded read before IO;
    refund only proven unread bytes after successful return. Failed reads retain
    the entire reservation because stdlib exceptions may omit consumed partials.
    Thus bytes_read is a conservative charge, not an exact network measurement.
    """
    max_requests: int = 10
    run_seconds: float = 120
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    max_bytes: int = 10 * MAX_BYTES
    bytes_read: int = field(default=0, init=False)

    def _validate(self):
        if (type(self.max_requests) is not int or not 1 <= self.max_requests <= 10
                or not isinstance(self.run_seconds, (int, float))
                or isinstance(self.run_seconds, bool) or not 0 < self.run_seconds <= 120
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 10 * MAX_BYTES):
            raise FetchError('Invalid research budget')

    def body_read_size(self, requested):
        self._validate()
        remaining = self.max_bytes - self.bytes_read
        if remaining <= 0:
            raise FetchError('Research response-body byte budget exhausted')
        return min(requested, remaining)

    def charge_body_bytes(self, size):
        if type(size) is not int or size < 0 or self.bytes_read + size > self.max_bytes:
            raise FetchError('Research response-body byte budget exceeded')
        self.bytes_read += size

    def reserve(self):
        self._validate()
        _remaining(self.started + self.run_seconds)
        if self.requests >= self.max_requests:
            raise FetchError('Research request budget exhausted')
        if self.bytes_read >= self.max_bytes:
            raise FetchError('Research response-body byte budget exhausted')
        self.requests += 1


@dataclass(frozen=True)
class AcquiredSource:
    requested_url: str
    final_url: str
    acquired_at: str
    media_type: str
    content_encoding: str
    parser_version: str
    original_bytes: bytes
    original_sha256: str
    canonical_text: str
    canonical_sha256: str
    provenance: str = 'anonymous_static_download'
    completeness: str = 'unassessed'


def _body(response, deadline, budget=None):
    encoding = (response.getheader('Content-Encoding') or 'identity').lower().strip()
    if encoding not in ('identity', 'gzip', 'deflate'):
        raise FetchError('Unsupported content encoding')
    length = response.getheader('Content-Length')
    transfer = response.getheader('Transfer-Encoding')
    if transfer is not None and (transfer.lower() != 'chunked' or length is not None):
        # HTTPResponse ignores CL when it recognizes chunking. Never use that
        # ignored header to accept a truncated prefix as the original source.
        # Match the parser's exact token handling; permissive whitespace stripping
        # could accept chunk framing the parser actually leaves undecoded.
        raise FetchError('Ambiguous or unsupported response framing')
    if length is not None:
        try:
            if (not length.strip() or any(char not in '0123456789' for char in length.strip())
                    or not 0 <= int(length) <= MAX_BYTES):
                raise ValueError()
        except ValueError:
            raise FetchError('Response byte limit exceeded or invalid length') from None
    decoder = (zlib.decompressobj(31 if encoding == 'gzip' else zlib.MAX_WBITS)
               if encoding != 'identity' else None)
    raw, decoded = bytearray(), bytearray()
    while True:
        _remaining(deadline)
        if length is not None and len(raw) == int(length):
            break
        size = min(64 * 1024, MAX_BYTES - len(raw) + 1)
        if budget:
            size = budget.body_read_size(size)
            budget.charge_body_bytes(size)
        # IncompleteRead.partial can omit nested chunk payload. Do not refund an
        # exceptional read or infer that no bytes arrived from a missing receipt.
        chunk = response.read(size)
        if budget:
            if not isinstance(chunk, bytes) or len(chunk) > size:
                raise FetchError('Invalid bounded response-body read')
            budget.bytes_read -= size - len(chunk)
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            raise FetchError('Response byte limit exceeded')
        try:
            decoded.extend(decoder.decompress(chunk, MAX_BYTES - len(decoded) + 1)
                           if decoder else chunk)
        except zlib.error:
            raise FetchError('Invalid compressed source') from None
        if len(decoded) > MAX_BYTES or (decoder and decoder.unconsumed_tail):
            raise FetchError('Decompressed response exceeds byte limit')
    if decoder and (not decoder.eof or decoder.unused_data):
        raise FetchError('Truncated or concatenated compressed source')
    if length is not None and len(raw) != int(length):
        raise FetchError('Incomplete source body')
    return bytes(raw), bytes(decoded), encoding


def acquire(url: str, confirmed_hosts: list[str], budget: FetchBudget) -> AcquiredSource:
    """Acquire one static source. Redirects consume request budget and revalidate DNS.

    No retry on connect failure; a changed address cannot bypass validation. Local
    source persistence/owner association and company/role coverage belong to S2/S3.
    """
    hosts = {_host(host) for host in confirmed_hosts}
    if not hosts or len(hosts) > 20:
        raise FetchError('Confirm a bounded exact host allowlist')
    requested, _, _ = validate_url(url, hosts)
    current = requested
    deadline = min(time.monotonic() + 15, budget.started + budget.run_seconds)
    for hop in range(MAX_REDIRECTS + 1):
        current, host, target = validate_url(current, hosts)
        budget.reserve()
        addresses = _resolve(host, deadline)
        # Validate injected/returned resolver output again at the connect boundary.
        if not addresses or len(addresses) > 64:
            raise FetchError('No bounded public address set')
        addresses = [_public_ip(address) for address in addresses]
        connection = _PinnedHTTPS(host, addresses[0], deadline)
        response = None
        try:
            connection.request('GET', target, headers={
                'Host': f'[{host}]' if ':' in host else host, 'User-Agent': 'ApplicationCopilot-StaticResearch/1',
                'Accept': 'text/html,text/plain,text/markdown,application/pdf',
                'Accept-Encoding': 'gzip,deflate', 'Connection': 'close',
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or hop == MAX_REDIRECTS:
                    raise FetchError('Redirect limit exceeded or missing destination')
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise FetchError('Public source unavailable')
            media = (response.getheader('Content-Type') or '').split(';', 1)[0].strip().lower()
            raw, decoded, encoding = _body(response, deadline, budget)
            try:
                text, parser = extract(decoded, media, timeout=_remaining(deadline))
            except ExtractionError as exc:
                raise FetchError(str(exc)) from None
            _remaining(deadline)
            return AcquiredSource(requested, current, datetime.now(UTC).isoformat(), media,
                                  encoding, parser, raw, hashlib.sha256(raw).hexdigest(),
                                  text, hashlib.sha256(text.encode('utf-8')).hexdigest())
        except (OSError, http.client.HTTPException, UnicodeError):
            raise FetchError('Verified HTTPS acquisition failed') from None
        finally:
            if response is not None:
                response.close()
            connection.close()
    raise FetchError('Redirect limit exceeded')
