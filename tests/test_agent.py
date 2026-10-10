# -*- coding: utf-8 -*-
"""What must not break silently.

Two of these carry disproportionate weight. The serialisation test pins the one
thing the bridge must never do -- convert a timestamp -- and the marker test
pins the coupling between the error classifier and the vendored library, which
would otherwise rot into "every failure is bridge_error" without a single
test going red.
"""
import contextlib
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from zkbridge import errors, i18n                       # noqa: E402
from zkbridge.device import serialise_attendance        # noqa: E402
from zkbridge.spool import Spool, chunk_records         # noqa: E402
from unittest import mock                               # noqa: E402
import zkbridge.device as device_mod                    # noqa: E402


class FakeRecord(object):
    def __init__(self, user_id, timestamp, status=1, punch=0):
        self.user_id = user_id
        self.timestamp = timestamp
        self.status = status
        self.punch = punch


class TestSerialisation(unittest.TestCase):

    def test_the_timestamp_is_naive_device_local_with_no_offset(self):
        """An offset would prove the bridge converted something. Odoo owns the
        conversion, and it owns it alone."""
        out = serialise_attendance([FakeRecord('37', datetime(2026, 9, 29, 7, 58, 14))])
        self.assertEqual(out[0]['timestamp'], '2026-09-29 07:58:14')
        self.assertNotIn('+', out[0]['timestamp'])
        self.assertNotIn('Z', out[0]['timestamp'])

    def test_every_field_survives_untouched(self):
        out = serialise_attendance([FakeRecord('A12', datetime(2026, 9, 29, 8, 3, 7),
                                               status=15, punch=1)])
        self.assertEqual(out[0], {'user_id': 'A12', 'timestamp': '2026-09-29 08:03:07',
                                  'status': 15, 'punch': 1})

    def test_a_missing_punch_stays_missing(self):
        """punch 0 is the common case and is falsy, so absence must be None and
        never conflated with check-in."""
        record = FakeRecord('7', datetime(2026, 9, 29, 8, 0, 0))
        record.punch = None
        self.assertIsNone(serialise_attendance([record])[0]['punch'])

    def test_the_result_is_json_serialisable(self):
        json.dumps(serialise_attendance([FakeRecord('7', datetime(2026, 9, 29, 8, 0, 0))]))


class TestErrorMarkers(unittest.TestCase):

    def test_every_marker_still_exists_in_the_vendored_library(self):
        """The library collapses every exception into one type, so
        classification matches on message text. If the vendored copy drifts,
        these strings stop matching and every failure silently becomes
        'bridge_error'."""
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / 'src' / 'zkbridge' / 'vendor' / 'zk' / 'base.py'
                  ).read_text(encoding='utf-8', errors='replace').lower()
        missing = []
        for code, markers in errors.MARKERS:
            if not any(marker in source for marker in markers):
                missing.append((code, markers))
        self.assertFalse(missing, 'no longer present in the vendored zk: %s' % missing)

    def test_a_preflight_failure_is_always_unreachable(self):
        code, _ = errors.classify(errors.PreflightError('nothing there'))
        self.assertEqual(code, errors.UNREACHABLE)

    def test_an_unknown_failure_is_never_silently_dropped(self):
        code, detail = errors.classify(RuntimeError('something new'))
        self.assertEqual(code, errors.BRIDGE_ERROR)
        self.assertIn('something new', detail)


class TestVendorPinned(unittest.TestCase):

    def test_the_vendored_library_matches_its_manifest(self):
        """A local edit here would diverge from the Odoo module's copy, which
        is the one difference nobody would think to look for."""
        root = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'zkbridge' / 'vendor'
        expected = {}
        for line in (root / 'ZK_SHA256SUMS').read_text().splitlines():
            digest, name = line.split(None, 1)
            expected[name.strip()] = digest
        for name, digest in expected.items():
            actual = hashlib.sha256((root / 'zk' / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, '%s has been edited' % name)


class TestTranslations(unittest.TestCase):

    def test_both_languages_carry_the_same_keys(self):
        """Every operator-facing string exists in both, or the page shows
        English to somebody who was promised Arabic."""
        english = set(i18n.STRINGS['en'])
        for code, table in i18n.STRINGS.items():
            self.assertEqual(set(table), english, 'keys differ in %s' % code)

    def test_arabic_reads_right_to_left(self):
        self.assertEqual(i18n.direction('ar'), 'rtl')
        self.assertEqual(i18n.direction('en'), 'ltr')


class TestSpool(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(pathlib.Path(self.tmp.name) / 'state.db')

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self):
        return {'job_id': 7, 'attempt': 1, 'lease_token': 'abc',
                'operation': 'pull_attendance'}

    def test_a_large_pull_is_split_and_kept_in_order(self):
        punches = [{'user_id': '1', 'timestamp': '2026-09-%02d 08:00:00' % (day % 28 + 1)}
                   for day in range(2500)]
        count = self.spool.add_result(self._job(), 'ok', {'identity': {}, 'punches': punches})
        self.assertEqual(count, 3)
        batch = self.spool.next_batch()
        self.assertEqual([row['chunk_index'] for row in batch], [0, 1, 2])

    def test_chunks_are_sorted_ascending(self):
        """Descending would advance Odoo's watermark across a gap."""
        groups = chunk_records([
            {'user_id': '1', 'timestamp': '2026-09-02 08:00:00'},
            {'user_id': '1', 'timestamp': '2026-09-01 08:00:00'}])
        self.assertEqual(groups[0][0]['timestamp'], '2026-09-01 08:00:00')

    def test_nothing_is_dropped_until_it_is_acknowledged(self):
        self.spool.add_result(self._job(), 'ok', {'identity': {}, 'punches': []})
        self.assertEqual(self.spool.stats()['pending'], 1)
        key = self.spool.next_batch()[0]['idempotency_key']
        self.spool.drop(key)
        self.assertEqual(self.spool.stats()['pending'], 0)

    def test_an_answer_overtakes_bulk_data(self):
        """An operator waiting on a test connection is never stuck behind a
        twelve-megabyte attendance backlog."""
        self.spool.add_result(self._job(), 'ok', {'identity': {}, 'punches': [
            {'user_id': '1', 'timestamp': '2026-09-01 08:00:00'}]})
        self.spool.add_result({'job_id': 8, 'attempt': 1, 'lease_token': 'x',
                               'operation': 'identity'}, 'ok', {'identity': {}})
        self.assertEqual(self.spool.next_batch()[0]['operation'], 'identity')

    def test_the_same_result_is_never_queued_twice(self):
        job = self._job()
        self.spool.add_result(job, 'failed', {}, 'device_unreachable', 'no answer')
        first = self.spool.next_batch()[0]['idempotency_key']
        self.spool.drop(first)
        self.spool.add_result(job, 'failed', {}, 'device_unreachable', 'no answer')
        # A fresh result_id, so this is a new submission rather than a replay.
        self.assertNotEqual(self.spool.next_batch()[0]['idempotency_key'], first)


class FakeDeviceUser(object):
    def __init__(self, uid, user_id, name='NN'):
        self.uid = uid
        self.user_id = user_id
        self.name = name
        self.privilege = 0


def fake_terminal(users=()):
    terminal = mock.MagicMock()
    terminal.get_users.return_value = list(users)
    terminal.get_time.return_value = datetime(2026, 10, 1, 9, 15, 7)
    terminal.get_device_name.return_value = 'K50 Pro'
    terminal.get_firmware_version.return_value = 'Ver 6.60'
    terminal.get_platform.return_value = 'ZLM60_TFT'
    terminal.get_serialnumber.return_value = 'SN-1'
    terminal.get_mac.return_value = '00:17:61:00:00:01'
    terminal.users, terminal.records, terminal.fingers = len(users), 0, 0
    return terminal


class TestIdentity(unittest.TestCase):

    def test_the_terminal_clock_is_read(self):
        """A clock twenty minutes fast produces attendance twenty minutes wrong
        and passes every other check. Reading it is the only way to see it."""
        info = device_mod.read_identity(fake_terminal())
        self.assertEqual(info['device_local_time'], '2026-10-01 09:15:07')

    def test_one_unsupported_getter_does_not_discard_the_others(self):
        terminal = fake_terminal()
        terminal.get_mac.side_effect = Exception('not supported on this firmware')
        info = device_mod.read_identity(terminal)
        self.assertFalse(info['device_mac'])
        self.assertEqual(info['device_model'], 'K50 Pro')


class TestPushUsers(unittest.TestCase):

    def test_a_partial_push_names_the_one_that_failed(self):
        """Eleven written and the twelfth refused is not 'sync failed'."""
        terminal = fake_terminal()
        terminal.set_user.side_effect = [None, Exception('device full')]
        out = device_mod.run_push_users(terminal, {'users': [
            {'uid': 1, 'user_id': '11', 'name': 'A'},
            {'uid': 2, 'user_id': '12', 'name': 'B'}]})
        self.assertEqual([r['ok'] for r in out['results']], [True, False])
        self.assertEqual(out['results'][1]['user_id'], '12')
        self.assertIn('device full', out['results'][1]['error'])


class TestDeleteUser(unittest.TestCase):

    def test_the_user_id_is_always_sent(self):
        """Omitting it makes the library re-read every user on the terminal
        first, turning a one-packet delete into a bulk read."""
        terminal = fake_terminal([FakeDeviceUser(1, '37')])
        device_mod.run_delete_user(terminal, {'user_id': '37'})
        terminal.delete_user.assert_called_once_with(user_id='37')

    def test_deleting_somebody_who_is_not_there_touches_nothing(self):
        terminal = fake_terminal([])
        out = device_mod.run_delete_user(terminal, {'user_id': '37'})
        terminal.delete_user.assert_not_called()
        self.assertFalse(out['was_present'])


class TestEnrol(unittest.TestCase):

    def test_an_unknown_user_is_refused_before_the_terminal_is_asked(self):
        """enroll_user for somebody the terminal does not know just returns
        False, which is indistinguishable from a real enrolment failure."""
        terminal = fake_terminal([])
        out = device_mod.run_enroll_finger(terminal, {'user_id': '37'})
        terminal.enroll_user.assert_not_called()
        self.assertFalse(out['was_present'])

    def test_both_identifiers_are_passed(self):
        terminal = fake_terminal([FakeDeviceUser(41, '37')])
        terminal.enroll_user.return_value = True
        out = device_mod.run_enroll_finger(terminal, {'user_id': '37', 'uid': 41})
        terminal.enroll_user.assert_called_once_with(uid=41, user_id='37')
        self.assertTrue(out['enrolled'])

    def test_a_failed_enrolment_takes_the_terminal_out_of_enrolment_mode(self):
        """Not optional. The library restores the socket and cancels the
        capture only on the path where it does not raise, so a timeout leaves
        the terminal asking every passer-by for a finger."""
        block = {'host': '10.0.0.5', 'port': 4370, 'timeout': 5}
        terminal = fake_terminal([FakeDeviceUser(41, '37')])
        terminal.enroll_user.side_effect = Exception('timed out')
        recovery = fake_terminal()
        sessions = []

        @contextlib.contextmanager
        def _session(requested):
            handle = terminal if not sessions else recovery
            sessions.append(requested)
            yield handle

        with mock.patch.object(device_mod, 'zk_session', _session):
            outcome, _data, code, _detail = device_mod.execute(
                {'job_id': 1, 'operation': 'enroll_finger',
                 'device': block, 'request': {'user_id': '37', 'uid': 41}})

        self.assertEqual(outcome, 'failed')
        self.assertEqual(len(sessions), 2, 'recovery must open a FRESH connection')
        recovery.cancel_capture.assert_called_once()
        recovery.verify_user.assert_called_once()
        recovery.reg_event.assert_called_once_with(0)

    def test_recovery_that_cannot_connect_does_not_mask_the_real_error(self):
        """If the terminal has gone away entirely, the operator must still be
        told why the enrolment failed -- not why the clean-up failed."""
        block = {'host': '10.0.0.5', 'port': 4370, 'timeout': 5}
        terminal = fake_terminal([FakeDeviceUser(41, '37')])
        terminal.enroll_user.side_effect = Exception('Unauthenticated')
        calls = []

        @contextlib.contextmanager
        def _session(requested):
            calls.append(requested)
            if len(calls) > 1:
                raise OSError('the terminal has gone')
            yield terminal

        with mock.patch.object(device_mod, 'zk_session', _session):
            outcome, _data, code, detail = device_mod.execute(
                {'job_id': 1, 'operation': 'enroll_finger',
                 'device': block, 'request': {'user_id': '37', 'uid': 41}})

        self.assertEqual(outcome, 'failed')
        self.assertEqual(code, errors.AUTH_FAILED, 'the enrolment error, not the cleanup one')
        self.assertIn('Unauthenticated', detail)

    def test_one_recovery_step_failing_does_not_stop_the_others(self):
        """The three calls are independent; a terminal that refuses one may
        still accept the one that actually clears the prompt."""
        block = {'host': '10.0.0.5', 'port': 4370, 'timeout': 5}
        terminal = fake_terminal()
        terminal.cancel_capture.side_effect = Exception('refused')

        @contextlib.contextmanager
        def _session(requested):
            yield terminal

        with mock.patch.object(device_mod, 'zk_session', _session):
            self.assertTrue(device_mod.enroll_recovery(block))
        terminal.verify_user.assert_called_once()


class TestBaseUrl(unittest.TestCase):

    def test_the_address_odoo_shows_is_accepted_as_pasted(self):
        """Odoo's form shows the full endpoint under 'What to enter on the PC';
        taking it at its word must not produce a doubled path and a 404."""
        from zkbridge.config import normalise_base_url
        for pasted in ('https://hr.example.com',
                       'https://hr.example.com/',
                       'https://hr.example.com/zkteco/bridge/v1',
                       'https://hr.example.com/zkteco/bridge/v1/',
                       ' https://hr.example.com/zkteco/bridge/v1/poll '):
            self.assertEqual(normalise_base_url(pasted), 'https://hr.example.com', pasted)


class TestCapabilities(unittest.TestCase):

    def test_everything_declared_can_actually_be_run(self):
        """The server never hands out a job the agent did not declare, so a
        capability with no handler would be a job that waits forever."""
        from zkbridge.version import CAPABILITIES
        for name in CAPABILITIES:
            self.assertIn(name, device_mod.OPERATIONS, name)


if __name__ == '__main__':
    unittest.main()
