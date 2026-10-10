"""Literal owned-process kill/restart matrix. Synthetic provider, loopback receiver only."""
import json
import os
import select
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from test_jobs import setup as setup
from test_provider import ready, send

from copilot.contracts import EvidenceError
from copilot.jobs import Jobs
from copilot.provider import Provider
from copilot.store import Store

CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
import http.client
import json
import time
from datetime import datetime
from uuid import uuid4
from copilot.store import Store
from copilot.jobs import Jobs
from copilot.provider import Provider, DefinitelyUnsent
from copilot.contracts import EvidenceError
from copilot.domain.contracts import Contract
root, owner, job_id, phase, now, port = sys.argv[2:]
store = Store(root)
jobs = Jobs(store, lambda: datetime.fromisoformat(now))
job = jobs.claim(str(uuid4()), lease_seconds=5, job_id=job_id)
assert job is not None

def pause(label):
    print(json.dumps({'ready': label, 'pid': __import__('os').getpid(), 'fence': job.fence}), flush=True)
    while True:
        time.sleep(60)

class Output(Contract):
    answer: str
    @classmethod
    def model_validate_json(cls, value, **kwargs):
        result = super().model_validate_json(value, **kwargs)
        if phase == 'after_response_before_commit':
            pause(phase)
        return result

def transport(payload, before_send):
    if phase == 'after_definitely_unsent':
        raise DefinitelyUnsent()
    before_send()
    connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=5)
    try:
        body = b'{"synthetic":true}'
        if phase == 'during_transport':
            connection.putrequest('POST', '/fake-provider')
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(body)))
            connection.endheaders()
            connection.send(body[:len(body) // 2])
            pause(phase)  # Parent proves partial receipt before killing this PID.
        else:
            connection.request('POST', '/fake-provider', body=body,
                               headers={'Content-Type': 'application/json'})
        if phase == 'after_receiver_acceptance':
            print(json.dumps({'ready': phase, 'pid': __import__('os').getpid(), 'fence': job.fence}), flush=True)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return json.loads(raw)

provider = Provider(jobs, lambda: 'synthetic-private-key', transport)
if phase == 'before_reservation':
    pause(phase)
if phase == 'after_prepared':
    reserve = provider.reserve
    def prepared(*args, **kwargs):
        result = reserve(*args, **kwargs)
        pause(phase)
        return result
    provider.reserve = prepared
try:
    provider.send(job.id, job.lease_owner, job.fence, 'draft', model='configured-pinned-model',
                  instructions='Use supplied evidence.', input_value={'evidence': 'Synthetic'},
                  output_model=Output, reserved_tokens=5000, max_output_tokens=2000)
except EvidenceError as exc:
    if phase == 'after_definitely_unsent' and exc.code == 'PROVIDER_UNSENT':
        pause(phase)
    raise
pause('after_committed')
'''


@pytest.mark.skipif(os.name != 'posix', reason='This qualification asserts literal POSIX SIGKILL/reap semantics')
@pytest.mark.parametrize('phase', ['before_reservation', 'after_prepared', 'during_transport',
                                  'after_receiver_acceptance', 'after_response_before_commit', 'after_committed',
                                  'after_definitely_unsent'])
def test_actual_provider_process_kill_matrix_preserves_budget_and_never_replays(setup, tmp_path, phase):
    provider, original = ready(setup, lambda *_: pytest.fail('Parent transport must not run'))
    store, owner, _, clock = setup
    received = []
    partial_body_observed = threading.Event()
    partial_bodies = []
    receiver_accepted = threading.Event()
    response_sent = threading.Event()
    release_response = threading.Event()
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            assert self.path == '/fake-provider'
            self.connection.settimeout(5)
            expected = b'{"synthetic":true}'
            length = int(self.headers['Content-Length'])
            assert length == len(expected)
            if phase == 'during_transport':
                prefix = self.rfile.read(length // 2)
                assert prefix == expected[:length // 2]
                partial_bodies.append(prefix)
                partial_body_observed.set()
                remainder = self.rfile.read(length - len(prefix))
                if len(remainder) != length - len(prefix):
                    return  # Killed client never supplied a complete request.
                assert prefix + remainder == expected
            else:
                assert self.rfile.read(length) == expected
            received.append(True)  # Parent-owned receiver outlives the killed client.
            receiver_accepted.set()
            if phase == 'after_receiver_acceptance':
                assert release_response.wait(5), 'Parent must release the held receiver during cleanup'
                return  # No response bytes before or after the accepted client is killed.
            body = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"answer":"Synthetic grounded output"}'}]}],
                'usage': {'total_tokens': 20}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            response_sent.set()
    receiver = HTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    clock.advance(6)  # Child claims a real new fence, never an invented old lease.
    sandbox = tmp_path / 'child-cwd'
    sandbox.mkdir()
    child = subprocess.Popen([sys.executable, '-I', '-u', '-c', CHILD,
        str(Path(__file__).resolve().parents[1]), str(store.root), owner, original.id,
        phase, clock.now.isoformat(), str(receiver.server_port)], cwd=sandbox,
        env={'HOME': str(sandbox), 'PATH': os.defpath, 'LANG': 'C.UTF-8'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready_fds, _, _ = select.select([child.stdout], [], [], 12)
        assert ready_fds, 'Owned child did not reach its bounded ready handshake'
        handshake = json.loads(child.stdout.readline())
        assert handshake['ready'] == phase and handshake['pid'] == child.pid
        if phase == 'during_transport':
            assert partial_body_observed.wait(2), 'Receiver must observe partial request bytes before SIGKILL'
            assert partial_bodies == [b'{"synthetic":true}'[:len(b'{"synthetic":true}') // 2]]
            assert received == []
        if phase == 'after_receiver_acceptance':
            assert receiver_accepted.wait(2), 'Receiver must accept the full request before SIGKILL'
            assert not response_sent.is_set(), 'Accepted request must be killed before any response'
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == -signal.SIGKILL and not stdout and not stderr
        assert len(received) == (1 if phase in ('after_receiver_acceptance', 'after_response_before_commit', 'after_committed') else 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        release_response.set()
        receiver.shutdown()
        receiver.server_close()
        thread.join(5)
    restarted = Store(store.root)
    clock.advance(6)
    jobs = Jobs(restarted, clock)
    transport_calls = []
    restored = Provider(jobs, lambda: 'synthetic-private-key', lambda *_: transport_calls.append(True))
    recovered = jobs.claim(str(uuid4()), lease_seconds=5, job_id=original.id)
    with restarted._read() as db:
        rows = db.execute('SELECT state,dispatched,response FROM provider_attempts WHERE job_id=?', (original.id,)).fetchall()
    if phase == 'before_reservation':
        assert rows == [] and recovered.fence > handshake['fence']
        assert restored.usage(owner)['reserved_calls'] == 0
    elif phase == 'after_definitely_unsent':
        assert rows[0][0] == 'definitely_unsent' and rows[0][1] == 0
        assert recovered.fence > handshake['fence']
        assert restored.usage(owner)['reserved_calls'] == 0
        with pytest.raises(EvidenceError):
            send(restored, recovered)
    elif phase == 'after_committed':
        assert rows[0][0] == 'completed' and recovered.fence > handshake['fence']
        assert send(restored, recovered).answer == 'Synthetic grounded output'
        assert restored.usage(owner)['reserved_calls'] == 1
    else:
        assert recovered is None and rows[0][0] == 'indeterminate'
        assert rows[0][1] == (0 if phase == 'after_prepared' else 1)
        assert rows[0][2] is None
        assert jobs.get(owner, original.id).state == 'indeterminate'
        assert restored.usage(owner)['reserved_tokens'] == 5000
        assert jobs.claim(str(uuid4()), job_id=original.id) is None
    assert transport_calls == []
    with pytest.raises(EvidenceError):
        jobs.commit_stage(original.id, original.lease_owner, original.fence, 'late', {'answer': 'not publishable'})
