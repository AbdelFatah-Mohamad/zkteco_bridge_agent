# -*- coding: utf-8 -*-
"""Logging, with the secrets taken out structurally rather than by discipline.

A filter on the *root* logger, seeded with the live token, the access code and
every device password the agent has seen. Relying on nobody ever writing the
wrong thing into a log call is how the wrong thing ends up in a support ticket.
"""

import logging
import logging.handlers
import threading

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5

_secrets = set()
_lock = threading.Lock()


def remember_secret(value):
    """Anything that must never appear in a log line."""
    if value and isinstance(value, str) and len(value) >= 4:
        with _lock:
            _secrets.add(value)


class RedactingFilter(logging.Filter):

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        with _lock:
            known = list(_secrets)
        for secret in known:
            if secret in message:
                message = message.replace(secret, '***')
                record.msg = message
                record.args = ()
        return True


def redact_job(job):
    """A job as it is safe to log: no password, no employee names."""
    block = dict(job.get('device') or {})
    block.pop('password', None)
    trimmed = dict(job)
    trimmed['device'] = block
    request = trimmed.get('request') or {}
    if isinstance(request, dict) and request.get('users'):
        trimmed['request'] = {'users': '%s user(s)' % len(request['users'])}
    return trimmed


def configure(path, level='INFO'):
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(name)s: %(message)s')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding='utf-8')
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass

    # Mirrored to stderr as well, so a bridge that dies before it can open its
    # own log file still leaves a trace in journald or the service wrapper.
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.setLevel(logging.WARNING)
    root.addHandler(stream)

    root.addFilter(RedactingFilter())
    for handler in root.handlers:
        handler.addFilter(RedactingFilter())

    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    return root


def tail(path, lines=200):
    try:
        with open(str(path), 'r', encoding='utf-8', errors='replace') as handle:
            return handle.readlines()[-lines:]
    except OSError:
        return []
