# -*- coding: utf-8 -*-
"""The outbound loop. The only thing that talks to Odoo.

All scheduling is on ``time.monotonic``. An office PC sleeps and resumes, and a
wall-clock scheduler answers a nine-hour resume with a storm of overdue polls.
"""

import hashlib
import json
import logging
import random
import threading
import time

import requests

from . import config as config_mod
from .version import CAPABILITIES, PROTOCOL_VERSION, VERSION, zk_fingerprint

_logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 900
MIN_INTERVAL, MAX_INTERVAL = 60, 3600
# While anything is in hand, ask again almost at once. Without this an operator
# who presses "Test Connection" in Odoo waits a quarter of an hour for an
# answer and concludes the feature is broken.
FAST_INTERVAL = 20
MAX_FAST_POLLS = 60
CONNECT_TIMEOUT, READ_TIMEOUT = 10, 120


class RateLimited(Exception):
    """Asked sooner than the server allows.

    Not a failure: it carries its own wait, and treating it as one would push
    the agent into the exponential backoff and make a bridge that is merely
    eager look like a bridge that is broken.
    """

    def __init__(self, retry_after):
        super().__init__('rate limited, retry in %ss' % retry_after)
        self.retry_after = retry_after


class Poller(object):

    def __init__(self, paths, engine, spool, status):
        self.paths = paths
        self.engine = engine
        self.spool = spool
        self.status = status          # a plain dict the web page reads
        self._stop = threading.Event()
        self._fast_polls = 0
        self._failures = 0
        self._zk_hash = zk_fingerprint()
        self._identity = config_mod.install_identity(paths)
        self._fresh_state = not (paths.state_dir / 'polled_once').exists()

    def stop(self):
        self._stop.set()

    # -- helpers ---------------------------------------------------

    def _settings(self):
        return config_mod.load_config(self.paths)

    def _session_args(self, values):
        verify = True
        if values.get('ca_bundle'):
            verify = values['ca_bundle']
        return {'timeout': (CONNECT_TIMEOUT, READ_TIMEOUT), 'verify': verify}

    def _url(self, values, path):
        base = (values.get('odoo_base_url') or '').rstrip('/')
        if not base:
            raise ValueError('no Odoo address configured')
        if base.startswith('http://') and not values.get('allow_insecure'):
            # The token is a bearer credential and the job payloads carry the
            # communication password of every terminal. Plain HTTP puts both on
            # the wire.
            raise ValueError('refusing to use plain HTTP; use https:// or set allow_insecure')
        return '%s/zkteco/bridge/v1/%s' % (base, path)

    def _headers(self, values):
        return {'Content-Type': 'application/json',
                'Authorization': 'Bearer %s' % values.get('token', '')}

    def _note(self, **fields):
        self.status.update(fields)

    # -- the cycle -------------------------------------------------

    def run(self):
        # Two hundred bridges installed from one image all restart after a
        # Windows patch Tuesday; without a deterministic offset they hit Odoo
        # on the same second.
        offset = int(hashlib.blake2s(
            self._identity['install_id'].encode()).hexdigest()[:4], 16) % DEFAULT_INTERVAL
        self._note(next_poll_in=min(offset, 30))
        self.engine.wait_for_poll(min(offset, 30))

        while not self._stop.is_set():
            values = self._settings()
            if not config_mod.is_configured(values):
                self._note(state='unconfigured', message='Not set up yet.')
                self.engine.wait_for_poll(10)
                continue
            try:
                interval = self._cycle(values)
            except RateLimited as limited:
                interval = limited.retry_after
                self._fast_polls = MAX_FAST_POLLS      # stop asking early
            except Exception as exc:
                self._failures += 1
                interval = min(DEFAULT_INTERVAL * (2 ** (self._failures - 1)), MAX_INTERVAL)
                _logger.warning('poll failed (%s): %s', self._failures, exc)
                self._note(state='error', message=str(exc)[:300])
            delay = interval * random.uniform(0.85, 1.15)
            self._note(next_poll_in=int(delay), next_poll_at=time.time() + delay)
            self.engine.wait_for_poll(delay)

    def _cycle(self, values):
        """Deliver what is owed, then ask for work. Returns the next delay."""
        # Undelivered results go first: they are answers somebody is waiting
        # for, and holding them behind new work is how a spool grows forever.
        self._deliver(values)
        body = self._poll(values)

        jobs = body.get('jobs') or []
        self.engine.remember_devices(jobs)
        for job in jobs:
            self.engine.submit(job)

        self._failures = 0
        self._fresh_state = False
        try:
            (self.paths.state_dir / 'polled_once').write_text('1', encoding='utf-8')
        except OSError:
            pass

        interval = int(body.get('poll_interval_seconds') or DEFAULT_INTERVAL)
        override = values.get('poll_interval_s')
        if override:
            interval = int(override)
        interval = max(MIN_INTERVAL, min(interval, MAX_INTERVAL))

        # The server states its own floor, so the fast path can be eager
        # without being refused -- asking faster than allowed would spend every
        # quick poll on a 429 and deliver nothing.
        floor = int(body.get('min_poll_seconds') or FAST_INTERVAL)
        busy = bool(jobs) or body.get('jobs_remaining') or self.engine.has_work() \
            or self.spool.stats()['pending']
        if busy and self._fast_polls < MAX_FAST_POLLS:
            self._fast_polls += 1
            return max(min(interval, FAST_INTERVAL), floor)
        self._fast_polls = 0
        return interval

    def _poll(self, values):
        payload = dict({
            'protocol': PROTOCOL_VERSION,
            'agent_version': VERSION,
            'install_id': self._identity['install_id'],
            'bridge_time_utc': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
            'zk_sha256': self._zk_hash,
            'capabilities': CAPABILITIES,
            # "I started with an empty state file": Odoo releases the leases
            # this bridge is still nominally holding instead of waiting them
            # out, which turns a re-image from a twenty-minute stall into
            # nothing.
            'abandoned': self._fresh_state,
            'device_health': self.engine.device_health(),
        }, **config_mod.describe_host())

        response = requests.post(
            self._url(values, 'poll'), data=json.dumps(payload),
            headers=self._headers(values), **self._session_args(values))
        return self._handle_poll_response(response)

    def _handle_poll_response(self, response):
        if response.status_code == 200:
            body = response.json()
            self._note(state='ok', message='', last_poll_at=time.time(),
                       bridge_name=(body.get('bridge') or {}).get('name'),
                       server_time=body.get('server_time_utc'))
            return body
        if response.status_code == 401:
            # A rotated token needs a person at the PC. Say so loudly and keep
            # asking slowly; do not guess and do not back off into silence.
            self._note(state='unauthorized',
                       message='Odoo rejected this token. Enter the new one in Settings.')
            raise RuntimeError('unauthorized')
        if response.status_code == 426:
            self._note(state='upgrade_required',
                       message='This bridge is too old for the server. Install a newer one.')
            raise RuntimeError('upgrade required')
        if response.status_code == 429:
            retry = int((response.json() or {}).get('retry_after_seconds') or 60)
            self._note(state='rate_limited', message='Asked too soon; waiting.')
            raise RateLimited(max(retry, 1))
        raise RuntimeError('poll answered HTTP %s' % response.status_code)

    def _deliver(self, values):
        """Send spooled chunks until Odoo stops accepting them."""
        url = self._url(values, 'result')
        for row in self.spool.next_batch():
            payload = {
                'protocol': PROTOCOL_VERSION,
                'job_id': row['job_id'],
                'lease_token': row['lease_token'],
                'attempt': row['attempt'],
                'chunk_index': row['chunk_index'],
                'chunk_count': row['chunk_count'],
                'idempotency_key': row['idempotency_key'],
                'outcome': row['outcome'],
                'error_code': row['error_code'],
                'error_message': row['error_message'],
                'data': json.loads(row['body']),
            }
            try:
                response = requests.post(
                    url, data=json.dumps(payload), headers=self._headers(values),
                    **self._session_args(values))
            except Exception as exc:
                # Network trouble: keep everything and try again next time.
                _logger.warning('could not deliver chunk %s: %s', row['chunk_index'], exc)
                return
            if not self._handle_result_response(row, response):
                return

    def _handle_result_response(self, row, response):
        """True to keep going, False to stop this cycle. Never drops a chunk on
        a 5xx: a 504 after Odoo committed is indistinguishable from one before,
        and the idempotency key is what makes that not matter."""
        code = response.status_code
        if code in (200, 202):
            self.spool.drop(row['idempotency_key'])
            return True
        if code in (400, 404, 409, 422):
            # Defects in the envelope, a job that no longer exists, a lease that
            # has moved on. Resending cannot help and would loop forever.
            _logger.warning('chunk %s of job %s refused (%s); discarding',
                            row['chunk_index'], row['job_id'], code)
            if code == 409 and (response.json() or {}).get('error') == 'chunk_out_of_order':
                return False          # the next batch starts where Odoo wants
            self.spool.drop(row['idempotency_key'])
            return True
        if code == 413:
            _logger.warning('chunk %s too large for the server; dropping the job',
                            row['chunk_index'])
            self.spool.drop_job(row['job_id'])
            return True
        if code in (401, 403, 426, 429):
            return False
        _logger.warning('result answered HTTP %s; keeping the chunk', code)
        return False
