# -*- coding: utf-8 -*-
"""What the bridge is doing right now, and the threads doing it.

One worker thread per device, created on demand. Per-device serialisation falls
out of that: a device has one queue and one consumer, so two jobs for the same
clock can never share a socket, while different clocks run in parallel.

Threads rather than asyncio for a specific reason, not a stylistic one: the
protocol library is blocking socket code that calls ``settimeout`` and ``recv``
directly, so under asyncio every device call becomes an executor call anyway,
plus an event loop to reason about.
"""

import copy
import logging
import queue
import threading
import time

from . import device as device_mod

_logger = logging.getLogger(__name__)

MAX_LOCAL_RESULTS = 20


class Engine(object):

    def __init__(self, spool, max_parallel=4):
        self.spool = spool
        self._lock = threading.RLock()
        self._queues = {}
        self._workers = {}
        self._running = {}
        self._queued = {}
        self._devices = {}
        self._local_results = []
        self._stop = threading.Event()
        self._wake_poller = threading.Event()
        self._slots = threading.Semaphore(max(1, int(max_parallel)))

    # -- lifecycle -------------------------------------------------

    def stop(self):
        self._stop.set()
        for q in list(self._queues.values()):
            q.put(None)

    def request_poll_now(self):
        self._wake_poller.set()

    def wait_for_poll(self, timeout):
        fired = self._wake_poller.wait(timeout)
        self._wake_poller.clear()
        return fired

    # -- submitting ------------------------------------------------

    def submit(self, job, source='odoo'):
        """Queue one job on its device's worker.

        Two jobs of the same kind for one device coalesce: two "pull
        attendance" requests are the same request, and running both would open
        the terminal twice for one answer.
        """
        key = self._device_key(job)
        with self._lock:
            pending = self._queued.setdefault(key, [])
            if source == 'odoo' and any(
                    other.get('operation') == job.get('operation') for other in pending):
                _logger.info('job %s superseded by an identical one already queued',
                             job.get('job_id'))
                return False
            pending.append(job)
            self._devices.setdefault(key, {}).update(
                name=(job.get('device') or {}).get('name') or key,
                device_id=(job.get('device') or {}).get('device_id'))
            self._ensure_worker(key)
        self._queues[key].put(job)
        return True

    def _device_key(self, job):
        block = job.get('device') or {}
        return '%s:%s' % (block.get('host'), block.get('port'))

    def _ensure_worker(self, key):
        if key in self._workers and self._workers[key].is_alive():
            return
        self._queues.setdefault(key, queue.Queue())
        worker = threading.Thread(target=self._work, args=(key,),
                                  name='dev:%s' % key, daemon=True)
        self._workers[key] = worker
        worker.start()

    # -- the worker ------------------------------------------------

    def _work(self, key):
        q = self._queues[key]
        while not self._stop.is_set():
            job = q.get()
            if job is None:
                return
            with self._lock:
                pending = self._queued.get(key) or []
                if job in pending:
                    pending.remove(job)
                self._running[key] = {
                    'job_id': job.get('job_id'), 'operation': job.get('operation'),
                    'started': time.time(), 'source': job.get('_source', 'odoo')}
            started = time.time()
            # An enrolment holds a terminal for minutes while somebody places a
            # finger on it. Letting it consume one of the few general slots
            # means one person at one clock starves the attendance pulls on
            # every other clock at the site, so it runs outside them.
            if job.get('operation') in device_mod.NEEDS_RECOVERY:
                outcome, data, code, detail = device_mod.execute(job)
            else:
                with self._slots:
                    outcome, data, code, detail = device_mod.execute(job)
            elapsed = int((time.time() - started) * 1000)
            with self._lock:
                self._running.pop(key, None)
                self._devices.setdefault(key, {}).update(
                    last_outcome=outcome, last_code=code, last_detail=detail,
                    last_run_at=time.time(), last_ms=elapsed,
                    reachable=(code != 'device_unreachable') if outcome != 'ok' else True,
                    identity=(data or {}).get('identity') or
                             self._devices.get(key, {}).get('identity'))

            if job.get('_source') == 'local':
                # A technician's test is not news for Odoo. Otherwise an hour of
                # debugging a cable fills the spool with noise Odoo has to ack.
                with self._lock:
                    self._local_results.insert(0, {
                        'job': job.get('operation'), 'device': key, 'outcome': outcome,
                        'code': code, 'detail': detail, 'ms': elapsed,
                        'identity': (data or {}).get('identity'), 'at': time.time()})
                    del self._local_results[MAX_LOCAL_RESULTS:]
            else:
                try:
                    self.spool.add_result(job, outcome, data, code, detail)
                except Exception:
                    _logger.exception('could not spool the result of job %s',
                                      job.get('job_id'))
            # Something is waiting on this: ask for the next poll straight away.
            self.request_poll_now()

    # -- reading ---------------------------------------------------

    def snapshot(self):
        with self._lock:
            return {
                'running': copy.deepcopy(list(self._running.values())),
                'queued': [{'device': key, 'operation': job.get('operation'),
                            'job_id': job.get('job_id')}
                           for key, jobs in self._queued.items() for job in jobs],
                'devices': copy.deepcopy(self._devices),
                'local_results': copy.deepcopy(self._local_results),
            }

    def has_work(self):
        with self._lock:
            return bool(self._running) or any(self._queued.values())

    def device_health(self):
        """Unsolicited observations, so an unplugged clock turns red in Odoo
        without anybody pressing a button."""
        with self._lock:
            health = []
            for info in self._devices.values():
                if not info.get('device_id'):
                    continue
                health.append({
                    'device_id': info['device_id'],
                    'reachable': bool(info.get('reachable', True)),
                    'error_code': info.get('last_code'),
                })
            return health

    def remember_devices(self, jobs):
        """Cache what Odoo says exists, so the local Devices page still works
        when Odoo is unreachable -- which is exactly when somebody needs it."""
        with self._lock:
            for job in jobs:
                block = job.get('device') or {}
                if not block.get('host'):
                    continue
                key = '%s:%s' % (block.get('host'), block.get('port'))
                self._devices.setdefault(key, {}).update(
                    name=block.get('name') or key,
                    device_id=block.get('device_id'),
                    block=block)
