# -*- coding: utf-8 -*-
"""Results that Odoo has not confirmed yet.

Nothing is deleted until a 2xx or a terminal 4xx arrives, so a crash, a reboot
or a week of no internet costs nothing. Deliberately at-least-once: exactly-once
over an office DSL line is a fiction, and here it is free -- the terminal buffer
is never cleared, so every pull re-sends the whole history anyway and Odoo's
deduplication is load-bearing regardless.
"""

import json
import logging
import sqlite3
import threading
import uuid

_logger = logging.getLogger(__name__)

MAX_CHUNKS = 5000
CHUNK_RECORDS = 1000
DISCARD_AFTER_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT    NOT NULL UNIQUE,
    job_id           INTEGER NOT NULL,
    lease_token      TEXT    NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    operation        TEXT    NOT NULL,
    chunk_index      INTEGER NOT NULL,
    chunk_count      INTEGER NOT NULL,
    outcome          TEXT    NOT NULL,
    error_code       TEXT,
    error_message    TEXT,
    body             TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    priority         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS chunk_order ON chunk (priority, id);
"""


def chunk_records(records, size=CHUNK_RECORDS):
    """Split ascending by (timestamp, user_id).

    Ascending is mandatory, not tidy: Odoo applies chunks in order and the
    direction fallback for terminals that report no usable punch code reads the
    previous punch out of the database. Descending would advance the watermark
    across a gap.
    """
    ordered = sorted(records, key=lambda r: (r['timestamp'], r['user_id']))
    if not ordered:
        return [[]]
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


class Spool(object):

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- writing ---------------------------------------------------

    def add_result(self, job, outcome, data, error_code=None, error_message=None):
        """Queue one job's answer, split into chunks where it is large.

        `result_id` is minted once per run of the job and reused across its
        chunks, so a retry of this attempt replays the same receipts while a
        genuinely new attempt is a new submission.
        """
        result_id = uuid.uuid4().hex
        job_id = int(job['job_id'])
        attempt = int(job.get('attempt') or 1)
        operation = job.get('operation') or ''

        if outcome != 'ok':
            bodies = [{}]
        elif operation == 'pull_attendance':
            groups = chunk_records(data.get('punches') or [])
            identity = data.get('identity') or {}
            # The identity rides on the first chunk only: repeating it on sixty
            # chunks is sixty identical writes.
            bodies = [{'identity': identity if index == 0 else {}, 'punches': group}
                      for index, group in enumerate(groups)]
        else:
            bodies = [data]

        count = len(bodies)
        with self._lock, self._connect() as conn:
            for index, body in enumerate(bodies):
                key = self._key(job_id, attempt, result_id, index)
                conn.execute(
                    'INSERT OR IGNORE INTO chunk (idempotency_key, job_id, lease_token, '
                    'attempt, operation, chunk_index, chunk_count, outcome, error_code, '
                    'error_message, body, priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key, job_id, job.get('lease_token') or '', attempt, operation,
                     index, count, outcome, error_code, error_message,
                     json.dumps(body, ensure_ascii=False),
                     0 if operation != 'pull_attendance' else 1))
        self.gc()
        return count

    @staticmethod
    def _key(job_id, attempt, result_id, index):
        import hashlib
        raw = 'zkbridge-result-v1|%s|%s|%s|%s' % (job_id, attempt, result_id, index)
        return hashlib.sha256(raw.encode()).hexdigest()

    # -- reading ---------------------------------------------------

    def next_batch(self, limit=8):
        """Oldest first, small answers before bulk data.

        The priority column is why: an operator waiting on a test connection is
        never stuck behind a twelve-megabyte attendance backlog.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM chunk ORDER BY priority, job_id, chunk_index, id LIMIT ?',
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def drop(self, idempotency_key):
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM chunk WHERE idempotency_key = ?', (idempotency_key,))

    def drop_job(self, job_id):
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM chunk WHERE job_id = ?', (int(job_id),))

    def stats(self):
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS pending, MIN(created_at) AS oldest FROM chunk').fetchone()
        return {'pending': row['pending'], 'oldest': row['oldest']}

    def gc(self):
        """Drop what is too old or too much, oldest first.

        Oldest first because the newest is what somebody is waiting for, and a
        dropped attendance chunk is re-pullable from the terminal buffer while
        a dropped answer is news nobody gets again.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM chunk WHERE created_at < datetime('now', ?)",
                ('-%d days' % DISCARD_AFTER_DAYS,))
            over = conn.execute('SELECT COUNT(*) FROM chunk').fetchone()[0] - MAX_CHUNKS
            if over > 0:
                conn.execute(
                    'DELETE FROM chunk WHERE id IN '
                    '(SELECT id FROM chunk ORDER BY id LIMIT ?)', (over,))
                _logger.warning('spool over capacity: dropped %s old chunk(s)', over)
