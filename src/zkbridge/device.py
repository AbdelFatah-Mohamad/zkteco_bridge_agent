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

from datetime import datetime

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
    # The terminal's own clock, read while connected anyway. A clock twenty
    # minutes fast produces attendance twenty minutes wrong and passes every
    # other check there is; reading it is the only way to see that.
    device_now = _safe(device.get_time, None)
    info = {
        # isinstance, not truthiness: whatever a firmware hands back has to end
        # up JSON-serialisable, because this dict IS the wire payload.
        'device_local_time': (device_now.strftime('%Y-%m-%d %H:%M:%S')
                              if isinstance(device_now, datetime) else False),
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


def run_push_users(device, request):
    """Write employees onto the terminal, one outcome each.

    set_user triggers a device-side refresh after *every* user, so three
    hundred employees is three hundred round trips over minutes. Reporting a
    single pass/fail for that would be useless: eleven written and the twelfth
    refused is not "sync failed", and somebody has to be told which twelfth.
    """
    present = {str(u.user_id) for u in (device.get_users() or [])}
    results = []
    for entry in request.get('users') or []:
        device_uid = str(entry.get('user_id'))
        try:
            device.set_user(int(entry.get('uid') or 0), entry.get('name') or '',
                            int(entry.get('privilege') or 0), '', '', device_uid)
            results.append({'user_id': device_uid, 'ok': True, 'error': None})
        except Exception as exc:
            results.append({'user_id': device_uid, 'ok': False,
                            'error': str(exc)[:200]})
    return {'users_on_device': sorted(present), 'results': results}


def run_delete_user(device, request):
    device_uid = str(request.get('user_id'))
    present = any(str(u.user_id) == device_uid for u in (device.get_users() or []))
    if present:
        # Always pass user_id; the library would otherwise re-read every user
        # on the terminal first, turning a one-packet delete into a bulk read.
        device.delete_user(user_id=device_uid)
    return {'user_id': device_uid, 'was_present': present, 'deleted': present}


def enroll_recovery(block):
    """Take the terminal out of enrolment mode after a failed enrol.

    This is not optional. enroll_user restores the socket timeout and calls
    cancel_capture / reg_event(0) / verify_user only on the path where it does
    *not* raise -- so a timeout or a refusal escapes before any of them run and
    leaves the terminal displaying "place finger" to every employee who walks
    up to it for the rest of the day.

    A fresh connection, never the poisoned handle: that one carries a sixty
    second timeout it never gave back.
    """
    try:
        with zk_session(block) as device:
            for call in (device.cancel_capture, lambda: device.reg_event(0),
                         device.verify_user):
                try:
                    call()
                except Exception:
                    _logger.debug('enrolment recovery step failed', exc_info=True)
        _logger.info('terminal %s:%s taken out of enrolment mode',
                     block.get('host'), block.get('port'))
        return True
    except Exception as exc:
        _logger.warning('could not take %s:%s out of enrolment mode: %s',
                        block.get('host'), block.get('port'), exc)
        return False


def run_enroll_finger(device, request):
    """Ask the terminal for a fingerprint. Blocks on a human.

    The library tries three times with a sixty second socket timeout on each of
    two reads, so the honest worst case is minutes, not one minute. Resolve the
    user first: enroll_user for someone the terminal does not know just returns
    False, which is indistinguishable from a genuine enrolment failure.
    """
    device_uid = str(request.get('user_id'))
    present = any(str(u.user_id) == device_uid for u in (device.get_users() or []))
    if not present:
        return {'user_id': device_uid, 'was_present': False, 'enrolled': False}
    uid = request.get('uid')
    enrolled = device.enroll_user(
        uid=int(uid) if uid else (int(device_uid) if device_uid.isdigit() else 0),
        user_id=device_uid)
    return {'user_id': device_uid, 'was_present': True, 'enrolled': bool(enrolled)}


OPERATIONS = {
    'identity': run_identity,
    'list_users': run_list_users,
    'pull_attendance': run_pull_attendance,
    'push_users': run_push_users,
    'delete_user': run_delete_user,
    'enroll_finger': run_enroll_finger,
}

# After these, the terminal may be left mid-operation and has to be put back.
NEEDS_RECOVERY = frozenset(['enroll_finger'])


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
        if operation in NEEDS_RECOVERY:
            # The terminal is probably still asking for a finger. Nobody at the
            # customer's office knows why, and it will keep asking.
            enroll_recovery(block)
        return 'failed', {}, code, detail
