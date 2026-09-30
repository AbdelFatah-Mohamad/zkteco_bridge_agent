# -*- coding: utf-8 -*-
"""What must not break silently.

Two of these carry disproportionate weight. The serialisation test pins the one
thing the bridge must never do -- convert a timestamp -- and the marker test
pins the coupling between the error classifier and the vendored library, which
would otherwise rot into "every failure is bridge_error" without a single
test going red.
"""
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


if __name__ == '__main__':
    unittest.main()
