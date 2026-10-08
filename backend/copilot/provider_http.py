"""Fixed-provider transport primitives with one absolute deadline and no keylogs.

Separate from anonymous research permissions: accepts no user URL, host, port or
credentials. DNS is killable; socket I/O bounds stdlib header/body parsing even
when HTTPConnection detaches a Connection:close response socket.
"""
import http.client
import io
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import time

HOST = 'api.openai.com'


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('Provider absolute deadline expired')
    return value


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return context


def resolve(deadline):
    script = ('import json,socket; '
              'a=socket.getaddrinfo("api.openai.com",443,type=socket.SOCK_STREAM); '
              'print(json.dumps(list(dict.fromkeys(x[4][0] for x in a))[:65]))')
    result = subprocess.run([sys.executable, '-I', '-c', script], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=remaining(deadline), check=False)
    remaining(deadline)
    addresses = json.loads(result.stdout) if result.returncode == 0 else None
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 64:
        raise ValueError('Provider DNS failed')
    validated = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_reserved or ip.is_multicast or (
                isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
            raise ValueError('Provider DNS returned an inadmissible address')
        validated.append(str(ip))
    # One selected connection attempt: no silent transport retries/fallback.
    return validated[0]


class DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline, release):
        self.sock, self.deadline, self.release = sock, deadline, release

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(remaining(self.deadline))
        result = self.sock.recv_into(buffer)
        remaining(self.deadline)
        return result

    def close(self):
        if not self.closed:
            super().close()
            self.release()


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.readers = 0
        self.closed = False

    def sendall(self, data):
        view = memoryview(data)
        while view:
            self.sock.settimeout(remaining(self.deadline))
            sent = self.sock.send(view)
            remaining(self.deadline)
            if not sent:
                raise ConnectionError('Provider transport closed')
            view = view[sent:]

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('Unsupported provider stream')
        self.readers += 1
        return io.BufferedReader(DeadlineReader(self.sock, self.deadline, self._release))

    def _release(self):
        self.readers -= 1
        if self.closed and not self.readers:
            self.sock.close()

    def close(self):
        if not self.closed:
            self.closed = True
            if not self.readers:
                self.sock.close()


class ProviderHTTPS(http.client.HTTPSConnection):
    def __init__(self, deadline):
        super().__init__(HOST, port=443, timeout=remaining(deadline), context=tls_context())
        self.deadline = deadline

    def connect(self):
        address = ipaddress.ip_address(resolve(self.deadline))
        raw = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        tls = None
        try:
            raw.settimeout(remaining(self.deadline))
            raw.connect((str(address), 443))
            raw.settimeout(remaining(self.deadline))
            tls = self._context.wrap_socket(raw, server_hostname=HOST)
            remaining(self.deadline)
            self.sock = DeadlineSocket(tls, self.deadline)
        except BaseException:
            if tls is not None:
                tls.close()
            else:
                raw.close()
            raise
