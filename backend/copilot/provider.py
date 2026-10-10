"""Explicit consent + durable reservations around one direct Responses attempt.

Injected transports are test surfaces, never model-generated tools. No retry,
provider-key persistence, prompt logging or asserted exact dollar pricing.
"""
import json
import time
from dataclasses import dataclass
from datetime import timezone

from pydantic import ValidationError

from .domain import contracts as c
from .domain.repository import uid
from .jobs import encode, error
from .provider_http import ProviderHTTPS, remaining


class DefinitelyUnsent(Exception):
    """Trusted transport established that no application request was transmitted."""


@dataclass(frozen=True)
class Limits:
    daily_tokens: int
    daily_calls: int
    job_tokens: int
    job_calls: int

    def __post_init__(self):
        for value in (self.daily_tokens, self.daily_calls, self.job_tokens, self.job_calls):
            if type(value) is not int or not 1 <= value <= 100_000_000:
                raise ValueError('Positive bounded integer budgets required')
        if self.job_tokens > self.daily_tokens or self.job_calls > self.daily_calls:
            raise ValueError('Job limits cannot exceed daily limits')


class ResponsesHTTP:
    """Fixed HTTPS endpoint, TLS verification, 45-second total deadline, no retries."""
    def __call__(self, payload, before_send):
        deadline = time.monotonic() + 45
        connection = ProviderHTTPS(deadline)
        response = None
        try:
            try:
                connection.connect()
                remaining(deadline)
            except Exception:
                raise DefinitelyUnsent() from None
            key = before_send()
            remaining(deadline)
            connection.request('POST', '/v1/responses', body=encode(payload).encode(), headers={
                'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            content = bytearray()
            while True:
                remaining(deadline)
                chunk = response.read1(min(65536, 2 * 1024 * 1024 + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > 2 * 1024 * 1024:
                    raise ValueError()
            if response.status != 200:
                # Do not expose provider response bodies, request IDs or key errors.
                raise RuntimeError()
            return json.loads(content)
        finally:
            if response is not None:
                response.close()
            connection.close()


def strict_output_schema(model):
    """Responses strict mode requires every declared object property to be required."""
    schema = model.model_json_schema()

    def visit(value):
        if isinstance(value, dict):
            value.pop('default', None)
            if value.get('type') == 'object' and 'properties' in value:
                value['additionalProperties'] = False
                value['required'] = list(value['properties'])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return schema


class Provider:
    def __init__(self, jobs, key_supplier, transport=None):
        self.jobs, self.store = jobs, jobs.store
        self._key_supplier = key_supplier
        self.transport = transport or ResponsesHTTP()

    def configure_limits(self, owner, limits: Limits):
        # Server/domain method only; no HTTP endpoint accepts arbitrary paid limits yet.
        with self.store._tx() as db:
            self.store._profile(db, owner)
            db.execute('INSERT INTO budget_limits VALUES(?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET '
                       'daily_tokens=excluded.daily_tokens,daily_calls=excluded.daily_calls,'
                       'job_tokens=excluded.job_tokens,job_calls=excluded.job_calls',
                       (owner, limits.daily_tokens, limits.daily_calls, limits.job_tokens, limits.job_calls))

    def _purpose_consent(self, db, job):
        purposes = {'research': 'research', 'draft': 'drafting', 'assess': 'assessment'}
        if job.kind not in purposes or not job.application_id:
            error('CONSENT_REQUIRED', 'This operation has no paid provider capability', 403)
        consent = self.jobs.domain._singleton(db, job.profile_id, 'consent', c.ConsentRecord)
        if (not consent or not consent.granted or consent.provider != 'openai'
                or purposes[job.kind] not in consent.purposes
                or consent.revision != job.revisions.consent):
            error('CONSENT_REQUIRED', 'Current explicit provider/purpose consent required', 403)

    def _family_attempts(self, db, job_id, exclude=None):
        return db.execute("""WITH RECURSIVE ancestors(id) AS (
            SELECT ? UNION SELECT r.prior_job_id FROM job_retries r JOIN ancestors a ON r.new_job_id=a.id),
            family(id) AS (SELECT id FROM ancestors UNION
            SELECT r.new_job_id FROM job_retries r JOIN family f ON r.prior_job_id=f.id)
            SELECT data FROM provider_attempts WHERE job_id IN (SELECT id FROM family)
            AND state!='definitely_unsent' AND id!=?""", (job_id, exclude or '')).fetchall()

    def request_retry(self, owner, prior_job_id, body):
        from .retrycontracts import WarnedRetry
        # Revalidate even trusted callers passing a model_copy with invalid fields.
        body = WarnedRetry.model_validate_json(body.model_dump_json())
        request_hash = c.canonical_hash({'operation': 'warned_provider_retry', 'prior_job_id': prior_job_id,
                                         'request': body.model_dump(mode='json')})
        with self.store._tx() as db:
            prior, parameters, dependencies = self.jobs._job(db, prior_job_id, owner)
            old = db.execute('SELECT job_id,request_sha256 FROM job_keys WHERE owner=? AND key=?',
                             (owner, body.idempotency_key)).fetchone()
            if old:
                if old[1] != request_hash:
                    error('CONFLICT', 'Idempotency key belongs to a different explicit retry')
                return self.jobs._job(db, old[0], owner)[0]
            if prior.kind not in ('research', 'draft', 'assess') or prior.state != 'indeterminate':
                error('CONFLICT', 'Only uncertain owned paid operations allow a warned retry')
            row = db.execute('SELECT data FROM provider_attempts WHERE id=? AND owner=? AND job_id=?',
                             (body.prior_attempt_id, owner, prior.id)).fetchone()
            attempt = c.ProviderAttempt.model_validate_json(row[0]) if row else None
            if (not attempt or attempt.state != 'indeterminate' or attempt.profile_id != owner
                    or attempt.application_id != prior.application_id or attempt.job_id != prior.id):
                error('NOT_FOUND', 'Owned uncertain provider attempt required', 404)
            current = self.jobs.capture(db, owner, prior.application_id)
            if not prior.revisions.matches(current, dependencies):
                error('CONFLICT', 'Captured inputs changed; start a new workflow rather than retry')
            self._purpose_consent(db, prior)
            limits = db.execute('SELECT daily_tokens,daily_calls,job_tokens,job_calls FROM budget_limits WHERE owner=?', (owner,)).fetchone()
            if not limits:
                error('BUDGET_REQUIRED', 'Configure explicit budgets before retry', 403)
            day = self.jobs._now().date().isoformat()
            daily = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND state!='definitely_unsent'", (owner, day)).fetchall()
            for rows, token_cap, call_cap in ((daily, limits[0], limits[1]),
                                             (self._family_attempts(db, prior.id), limits[2], limits[3])):
                attempts = [c.ProviderAttempt.model_validate_json(row[0]) for row in rows]
                if (sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in attempts) + attempt.reserved_tokens > token_cap
                        or len(attempts) + 1 > call_cap):
                    error('BUDGET_EXCEEDED', 'Original uncertainty and retry exceed configured budget', 403)
            return self.jobs._create_warned_retry(db, prior, parameters, dependencies, body, request_hash)

    def _authorize(self, db, job_id, worker_id, fence):
        job, _ = self.jobs.assert_current(db, job_id, worker_id, fence)
        self._purpose_consent(db, job)
        key = self._key_supplier()
        if not isinstance(key, str) or not key or len(key) > 4096 or any(ch in key for ch in '\r\n'):
            error('PROVIDER_UNCONFIGURED', 'Configure the provider explicitly before paid work', 503)
        return job, key

    def usage(self, owner):
        with self.store._tx() as db:
            self.store._profile(db, owner)
            rows = [c.ProviderAttempt.model_validate_json(row[0]) for row in db.execute(
                'SELECT data FROM provider_attempts WHERE owner=?', (owner,))]
            return {'schema_version': 1, 'reserved_tokens': sum(a.reserved_tokens for a in rows if a.state != 'definitely_unsent'),
                    'reserved_calls': sum(a.reserved_calls for a in rows if a.state != 'definitely_unsent'),
                    'reported_tokens': sum(a.reported_tokens or 0 for a in rows),
                    'unknown_attempts': sum(a.state in ('prepared', 'indeterminate') or a.reported_tokens is None
                                            for a in rows if a.state != 'definitely_unsent'),
                    'monetary_cost': None, 'pricing_status': 'unknown'}

    def attempts(self, owner, job_id):
        """Content-free reconciliation view; no assertion of externally resolved billing."""
        with self.store._read() as db:
            self.jobs._job(db, job_id, owner)
            result = []
            for serialized in db.execute('SELECT data FROM provider_attempts WHERE owner=? AND job_id=? ORDER BY rowid', (owner, job_id)):
                attempt = c.ProviderAttempt.model_validate_json(serialized[0])
                item = attempt.model_dump(mode='json', include={'id', 'job_id', 'fence', 'state',
                    'reserved_tokens', 'reserved_calls', 'reported_tokens', 'prepared_at'})
                item['reconciliation'] = 'unresolved' if attempt.state in ('prepared', 'indeterminate') else 'not_required'
                lineage = db.execute('SELECT prior_attempt_id FROM provider_retry_links WHERE attempt_id=?', (attempt.id,)).fetchone()
                item['retry_of_attempt_id'] = lineage[0] if lineage else None
                result.append(item)
        return result

    def reserve(self, job_id, worker_id, fence, stage, reserved_tokens, model, request_sha256=None):
        if (type(reserved_tokens) is not int or not 1 <= reserved_tokens <= 100_000_000
                or not isinstance(stage, str) or not 1 <= len(stage) <= 200
                or not isinstance(model, str) or not 1 <= len(model) <= 100):
            error('INVALID_INPUT', 'Provider reservation exceeds bounds', 422)
        request_sha256 = request_sha256 or c.canonical_hash({'model': model, 'reserved_tokens': reserved_tokens})
        with self.store._tx() as db:
            job, _ = self._authorize(db, job_id, worker_id, fence)
            old = db.execute('SELECT data,request_sha256 FROM provider_attempts WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
            if old:
                attempt = c.ProviderAttempt.model_validate_json(old[0])
                if attempt.model_version != model or attempt.reserved_tokens != reserved_tokens or old[1] != request_sha256:
                    error('CONFLICT', 'Provider stage already has different reserved inputs')
                return attempt
            limits = db.execute('SELECT daily_tokens,daily_calls,job_tokens,job_calls FROM budget_limits WHERE owner=?', (job.profile_id,)).fetchone()
            if not limits:
                error('BUDGET_REQUIRED', 'Configure explicit token and call budgets before paid work', 403)
            day = self.jobs._now().astimezone(timezone.utc).date().isoformat()
            daily = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND state!='definitely_unsent'", (job.profile_id, day)).fetchall()
            per_job = self._family_attempts(db, job.id)
            for rows, token_cap, call_cap in ((daily, limits[0], limits[1]), (per_job, limits[2], limits[3])):
                attempts = [c.ProviderAttempt.model_validate_json(r[0]) for r in rows]
                tokens = sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in attempts)
                if tokens + reserved_tokens > token_cap or len(attempts) + 1 > call_cap:
                    error('BUDGET_EXCEEDED', 'Provider token/call reservation exceeds configured limit', 403)
            attempt = c.ProviderAttempt(id=uid(), profile_id=job.profile_id, application_id=job.application_id,
                                         job_id=job.id, fence=fence, reserved_tokens=reserved_tokens,
                                         provider='openai', model_version=model,
                                         consent_revision=job.revisions.consent, state='prepared',
                                         prepared_at=self.jobs._now())
            db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (attempt.id, job.profile_id, job.id, attempt.state, attempt.model_dump_json(), 0, day, None, stage, request_sha256))
            lineage = db.execute('SELECT prior_attempt_id FROM job_retries WHERE new_job_id=?', (job.id,)).fetchone()
            if lineage:
                db.execute('INSERT INTO provider_retry_links VALUES(?,?)', (attempt.id, lineage[0]))
            return attempt

    def _before_send(self, attempt_id, job_id, worker_id, fence):
        with self.store._tx() as db:
            _, key = self._authorize(db, job_id, worker_id, fence)
            row = db.execute('SELECT data,dispatched FROM provider_attempts WHERE id=? AND job_id=?', (attempt_id, job_id)).fetchone()
            if not row or row[1] or c.ProviderAttempt.model_validate_json(row[0]).state != 'prepared':
                error('PROVIDER_UNKNOWN', 'This provider attempt cannot be transmitted again')
            attempt = c.ProviderAttempt.model_validate_json(row[0])
            day = self.jobs._now().date().isoformat()
            limits = db.execute('SELECT daily_tokens,daily_calls,job_tokens,job_calls FROM budget_limits WHERE owner=?', (attempt.profile_id,)).fetchone()
            if not limits:
                error('BUDGET_REQUIRED', 'Provider budget is no longer configured', 403)
            other_rows = db.execute("SELECT data FROM provider_attempts WHERE owner=? AND day=? AND id!=? AND state!='definitely_unsent'",
                                    (attempt.profile_id, day, attempt_id)).fetchall()
            others = [c.ProviderAttempt.model_validate_json(r[0]) for r in other_rows]
            if (sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in others) + attempt.reserved_tokens > limits[0]
                    or len(others) + 1 > limits[1]):
                error('BUDGET_EXCEEDED', 'Transmission-day reservation exceeds configured daily limit', 403)
            family = [c.ProviderAttempt.model_validate_json(row[0]) for row in self._family_attempts(db, job_id, attempt_id)]
            if (sum(max(a.reserved_tokens, a.reported_tokens or 0) for a in family) + attempt.reserved_tokens > limits[2]
                    or len(family) + 1 > limits[3]):
                error('BUDGET_EXCEEDED', 'Retry-family reservation exceeds configured job limit', 403)
            # Only undispatched attempts move: the serialized transaction proves
            # no previous body admission, retaining uncertain attempts untouched.
            db.execute('UPDATE provider_attempts SET dispatched=1,day=? WHERE id=?', (day, attempt_id))
            return key

    def _unknown(self, attempt_id, job_id):
        # Conservative recovery-only transition: never publishes a job result.
        with self.store._tx() as db:
            row = db.execute('SELECT data FROM provider_attempts WHERE id=? AND job_id=?', (attempt_id, job_id)).fetchone()
            uncertain_fence = None
            if row:
                attempt = c.ProviderAttempt.model_validate_json(row[0])
                if attempt.state in ('prepared', 'indeterminate'):
                    uncertain_fence = attempt.fence
                if attempt.state == 'prepared':
                    updated = attempt.model_copy(update={'state': 'indeterminate'})
                    db.execute('UPDATE provider_attempts SET state=?,data=? WHERE id=?', ('indeterminate', updated.model_dump_json(), attempt_id))
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row:
                job = c.DurableJob.model_validate_json(row[0])
                if job.state in ('queued', 'running') and job.fence == uncertain_fence:
                    self.jobs._save(db, job.model_copy(update={'state': 'indeterminate', 'stage': 'provider_unknown'}))

    def _unsent(self, attempt_id, job_id, worker_id, fence):
        with self.store._tx() as db:
            job, _, _ = self.jobs._job(db, job_id)
            row = db.execute('SELECT data,dispatched FROM provider_attempts WHERE id=? AND job_id=?', (attempt_id, job_id)).fetchone()
            if not row or row[1] or job.fence != fence or job.lease_owner != worker_id:
                return False
            attempt = c.ProviderAttempt.model_validate_json(row[0])
            if attempt.state != 'prepared':
                return False
            updated = attempt.model_copy(update={'state': 'definitely_unsent'})
            db.execute('UPDATE provider_attempts SET state=?,data=? WHERE id=?', ('definitely_unsent', updated.model_dump_json(), attempt_id))
            return True

    def send(self, job_id, worker_id, fence, stage, *, model, instructions, input_value,
             output_model, reserved_tokens, max_output_tokens=2000):
        if (not isinstance(instructions, str) or not 1 <= len(instructions) <= 100_000
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8000):
            error('INVALID_INPUT', 'Provider payload exceeds bounds', 422)
        payload = {'model': model, 'store': False, 'max_output_tokens': max_output_tokens,
                   'instructions': instructions, 'input': encode(input_value),
                   'text': {'format': {'type': 'json_schema', 'name': output_model.__name__,
                                       'strict': True, 'schema': strict_output_schema(output_model)}}}
        payload_bytes = len(encode(payload).encode())
        if payload_bytes > 131072:
            error('LIMIT_EXCEEDED', 'Provider payload exceeds bounds', 413)
        # Deliberately conservative UTF-8-byte input estimate plus output ceiling;
        # reservation is not a claim of exact tokenizer/monetary billing knowledge.
        if type(reserved_tokens) is not int or reserved_tokens < payload_bytes + max_output_tokens:
            error('INVALID_INPUT', 'Reservation must cover conservative input estimate and output ceiling', 422)
        attempt = self.reserve(job_id, worker_id, fence, stage, reserved_tokens, model,
                               request_sha256=c.canonical_hash(payload))
        if attempt.state == 'completed':
            with self.store._tx() as db:
                self.jobs.assert_current(db, job_id, worker_id, fence)
                row = db.execute('SELECT response FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()
                if row and row[0]:
                    return output_model.model_validate_json(row[0])
            error('INVALID_PROVIDER_OUTPUT', 'Completed provider attempt has no reusable validated output', 502)
        if attempt.state != 'prepared':
            error('PROVIDER_UNKNOWN', 'Resolve the previous provider attempt explicitly; no automatic retry', 409)
        try:
            response = self.transport(payload, lambda: self._before_send(attempt.id, job_id, worker_id, fence))
        except DefinitelyUnsent:
            if self._unsent(attempt.id, job_id, worker_id, fence):
                error('PROVIDER_UNSENT', 'Provider request was not sent; no successful result claimed', 503)
            self._unknown(attempt.id, job_id)
            error('PROVIDER_UNKNOWN', 'Provider outcome is unknown; reservation retained and no automatic retry', 502)
        except Exception:
            self._unknown(attempt.id, job_id)
            error('PROVIDER_UNKNOWN', 'Provider outcome is unknown; reservation retained and no automatic retry', 502)
        try:
            if not isinstance(response, dict) or response.get('status') != 'completed' or response.get('error'):
                raise ValueError()
            output = response.get('output')
            if not isinstance(output, list) or not output:
                raise ValueError()
            texts = []
            for item in output:
                if not isinstance(item, dict) or item.get('type') not in ('message', 'reasoning'):
                    raise ValueError()
                for content in item.get('content', []):
                    if (not isinstance(content, dict) or content.get('type') != 'output_text'
                            or not isinstance(content.get('text'), str) or content.get('refusal')):
                        raise ValueError()
                    texts.append(content['text'])
            validated = output_model.model_validate_json(''.join(texts))
            usage = response.get('usage')
            reported = usage.get('total_tokens') if isinstance(usage, dict) else None
            if reported is not None and (type(reported) is not int or not 0 <= reported <= 100_000_000):
                raise ValueError()
        except (ValueError, TypeError, ValidationError):
            self._unknown(attempt.id, job_id)
            error('INVALID_PROVIDER_OUTPUT', 'Provider returned incomplete, refused or invalid structured output', 502)
        try:
            with self.store._tx() as db:
                self._authorize(db, job_id, worker_id, fence)
                row = db.execute('SELECT data,dispatched FROM provider_attempts WHERE id=?', (attempt.id,)).fetchone()
                current = c.ProviderAttempt.model_validate_json(row[0])
                if current.state != 'prepared' or not row[1]:
                    error('PROVIDER_UNKNOWN', 'Provider attempt no longer owns completion authority')
                completed = current.model_copy(update={'state': 'completed', 'reported_tokens': reported})
                db.execute('UPDATE provider_attempts SET state=?,data=?,response=? WHERE id=?',
                           ('completed', completed.model_dump_json(), validated.model_dump_json(), attempt.id))
        except Exception:
            self._unknown(attempt.id, job_id)
            error('PROVIDER_UNKNOWN', 'Provider response could not be committed under current authority', 502)
        return validated
