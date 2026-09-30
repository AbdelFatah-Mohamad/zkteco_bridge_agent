# -*- coding: utf-8 -*-
"""Talking to a terminal.

``zk_session`` mirrors the module's ``_zk_session`` exactly, including
``ommit_ping=True``: that is what keeps a bug report from a bridged site
comparable with one from a directly-dialled site, and it also means no ping
subprocess is ever spawned on a customer's PC and the systemd unit can drop
every capability.
"""

import logging
import socket

from contextlib import contextmanager

from .errors import IDENTITY_MISMATCH, PAYLOAD_REJECTED, PreflightError, classify
from .vendor.zk import ZK

_logger = logging.getLogger(__name__)

PREFLIGHT_TIMEOUT = 5
MIN_TIMEOUT, MAX_TIMEOUT = 3, 60


def preflight(host, port, timeout=PREFLIGHT_TIMEOUT):
    """Is anything listening at all?

    Done before ZK.connect because the library's own probe hardcodes a ten
    second timeout that no configuration can lower, and because it is the only
    signal that separates "unplugged" from "answered oddly".
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        raise PreflightError('%s:%s did not answer (%s)' % (host, port, exc))


@contextmanager
def zk_session(block):
    """A connected handle for one job, always disconnected afterwards.

    One connection per job and never reused across jobs: firmwares drop idle
    sessions silently, so a reused handle fails on the *next* command with an
    error indistinguishable from a real network fault -- and enroll_user
    mutates the socket timeout to 60 s and restores it only when it does not
    raise.
    """
    host = block.get('host')
    port = int(block.get('port') or 4370)
    if not host:
        raise ValueError('the job carries no device address')
    timeout = max(MIN_TIMEOUT, min(int(block.get('timeout') or 20), MAX_TIMEOUT))

    preflight(host, port, timeout=min(timeout, PREFLIGHT_TIMEOUT))
    device = ZK(
        host,
        port=port,
        timeout=timeout,
        password=int(block.get('password') or 0),
        force_udp=bool(block.get('force_udp')),
        ommit_ping=bool(block.get('ommit_ping', True)),
    )
    connection = None
    try:
        connection = device.connect()
        yield device
    finally:
        if connection:
            try:
                device.disconnect()
            except Exception:
                _logger.debug('error disconnecting from %s:%s', host, port, exc_info=True)


def _safe(getter, default=False):
    """Several firmwares do not answer get_mac or get_platform. Letting one
    raise would discard the four that worked."""
    try:
        return getter()
    except Exception:
        return default


def read_identity(device):
    info = {
        'device_model': _safe(device.get_device_name) or False,
        'firmware_version': _safe(device.get_firmware_version) or False,
        'platform': _safe(device.get_platform) or False,
        'serial_number': _safe(device.get_serialnumber) or False,
        'device_mac': _safe(device.get_mac) or False,
    }
    try:
        device.read_sizes()
        info.update(users=device.users, records=device.records, fingers=device.fingers)
    except Exception:
        info.update(users=None, records=None, fingers=None)
    return info


def _check_serial(device, expected):
    """Refuse to touch a terminal that is not the one Odoo meant.

    The same private address on two different networks is two different
    machines, and writing one network's punches against the other's employees
    is the worst thing that can happen here.
    """
    if not expected:
        return
    found = _safe(device.get_serialnumber) or ''
    if found and str(found).strip() != str(expected).strip():
        raise LookupError('expected serial %s but the terminal reports %s'
                          % (expected, found))


def serialise_attendance(records):
    """The punches, exactly as the terminal gave them.

    The timestamp stays naive device-local wall clock with no offset: Odoo owns
    the conversion, and an offset here would prove the bridge converted
    something. The bridge has no timezone library at all, which is both a
    smaller dependency surface and a structural guarantee.
    """
    punches = []
    for record in records or []:
        punch = getattr(record, 'punch', None)
        punches.append({
            'user_id': str(record.user_id),
            'timestamp': record.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'status': None if record.status is None else int(record.status),
            'punch': None if punch is None else int(punch),
        })
    return punches


# --- the operations this build supports ------------------------------------

def run_identity(device, request):
    return {'identity': read_identity(device)}


def run_list_users(device, request):
    return {'users': [
        {'uid': int(u.uid or 0), 'user_id': str(u.user_id or '').strip(),
         'name': u.name or '', 'privilege': int(u.privilege or 0)}
        for u in (device.get_users() or [])]}


def run_pull_attendance(device, request):
    # get_attendance internally re-reads the sizes and the users, so one "pull"
    # is three conversations. Nothing here clears the device buffer, and nothing
    # ever should: the terminal keeping its whole history is what makes a week
    # of bridge downtime a bandwidth problem instead of a data-loss problem.
    identity = read_identity(device)
    return {'identity': identity, 'punches': serialise_attendance(device.get_attendance())}


OPERATIONS = {
    'identity': run_identity,
    'list_users': run_list_users,
    'pull_attendance': run_pull_attendance,
}


def execute(job):
    """Run one job. Returns (outcome, data, error_code, error_detail)."""
    operation = job.get('operation')
    handler = OPERATIONS.get(operation)
    if handler is None:
        return 'failed', {}, PAYLOAD_REJECTED, 'this bridge cannot do %r' % operation

    block = job.get('device') or {}
    try:
        with zk_session(block) as device:
            _check_serial(device, block.get('expected_serial'))
            return 'ok', handler(device, job.get('request') or {}), None, None
    except LookupError as exc:
        return 'failed', {}, IDENTITY_MISMATCH, str(exc)
    except ValueError as exc:
        return 'failed', {}, PAYLOAD_REJECTED, str(exc)
    except Exception as exc:
        code, detail = classify(exc)
        return 'failed', {}, code, detail
