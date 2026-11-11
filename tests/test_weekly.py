import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_token_monitor.telemetry import Monitor, normalize_limit
from codex_token_monitor.ui import _Dashboard
from codex_token_monitor.weekly import WEEK, WeeklyHistory


def quota(start, at, slot='primary', bucket='codex'):
    return dict(type='event_msg', timestamp=at, payload=dict(type='token_count',
                rate_limits=dict(limit_id=bucket, **{slot: dict(
                    used_percent=30, window_minutes=10080, resets_at=start + WEEK)})))


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'history.jsonl'
        self.path.touch()
        self.history = WeeklyHistory()

    def append(self, *events):
        with self.path.open('a') as f:
            for event in events:
                f.write(json.dumps(event) + '\n')

    def test_normal_early_and_missing_cycles_have_distinct_boundaries(self):
        first, manual = 100, 1000
        current = 3 * WEEK + 1000
        self.append(quota(first, 101), quota(manual, 1001, 'secondary'),
                    quota(current, current + 1), quota(current, current + 2, bucket='other'))
        periods, warnings = self.history.periods([self.path], [], current + 20)
        self.assertEqual([(p['start'], p['end'], p['current']) for p in periods],
                         [(current, current + 20, True), (manual, manual + WEEK, False),
                          (first, manual, False)])
        self.assertTrue(periods[-1]['early_reset'])
        self.assertFalse(periods[-2]['early_reset'])
        self.assertEqual(warnings, [])

    def test_reset_timestamp_jitter_is_one_cycle_and_new_lines_are_incremental(self):
        self.append(quota(100, 150), quota(101, 151), quota(140, 152))
        periods, _ = self.history.periods([self.path], [], 160)
        self.assertEqual(len(periods), 1)
        offset = self.history.files[str(self.path)]['offset']
        self.history.periods([self.path], [], 161)
        self.assertEqual(self.history.files[str(self.path)]['offset'], offset)
        self.append(quota(1000, 1001))
        periods, _ = self.history.periods([self.path], [], 1020)
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[-1]['end'], 1000)

    def test_truncated_history_drops_removed_cycles(self):
        self.append(quota(100, 101), quota(1000, 1001))
        self.history.periods([self.path], [], 250)
        self.path.write_text(json.dumps(quota(1000, 1001)) + '\n')
        periods, _ = self.history.periods([self.path], [], 1050)
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]['start'], 1000)

    def test_partial_lines_are_retried_and_live_current_is_available(self):
        self.path.write_text(json.dumps(quota(100, 150)))
        self.assertEqual(self.history.periods([self.path], [], 160)[0], [])
        with self.path.open('a') as f:
            f.write('\n')
        self.assertEqual(len(self.history.periods([self.path], [], 160)[0]), 1)
        live = normalize_limit(dict(limitId='codex', primary=dict(
            usedPercent=5, windowDurationMins=10080, resetsAt=1000 + WEEK)),
            1010, 'account/rateLimits/read')
        periods, _ = self.history.periods([self.path], [live], 1020)
        self.assertEqual(periods[0]['start'], 1000)
        self.assertTrue(periods[0]['current'])

    def test_picker_selection_and_previous_next_do_not_jump_to_current(self):
        current = dict(start=200, end=300, reset_at=200 + WEEK, current=True, early_reset=False)
        previous = dict(start=100, end=200, reset_at=100 + WEEK, current=False, early_reset=True)
        dashboard = _Dashboard(None, None, 'chat', 5, 2)
        dashboard.key('w')
        self.assertEqual(dashboard.request_key(), ('weekly_picker',))
        dashboard.weekly_periods = [current, previous]
        dashboard.key('j')
        dashboard.key('\n')
        self.assertEqual(dashboard.request_key(), ('time', 'weekly', previous['reset_at']))
        dashboard.data = dict(weekly_period=previous, weekly_periods=[current, previous])
        dashboard.key(']')
        self.assertEqual(dashboard.request_key(), ('time', 'weekly', None))
        dashboard.data = dict(weekly_period=current, weekly_periods=[current, previous])
        dashboard.key('[')
        self.assertEqual(dashboard.weekly_reset, previous['reset_at'])
        dashboard.key('5')
        self.assertIsNone(dashboard.weekly_reset)

    def test_monitor_uses_selected_history_dates_instead_of_last_seven_days(self):
        monitor = Monitor(self.home)
        selected = dict(start=100, end=200, reset_at=100 + WEEK, current=False)
        with patch.object(monitor, 'weekly_periods', return_value=dict(periods=[selected], warnings=[])):
            with patch.object(monitor, 'period_snapshot', return_value=dict(warnings=[])) as snapshot:
                result = monitor.weekly_snapshot(selected['reset_at'])
                snapshot.assert_called_once_with(100, 200)
                self.assertEqual(result['weekly_period'], selected)
            missing = monitor.weekly_snapshot(999999999)
            self.assertIsNone(missing['start'])
            self.assertFalse(missing['api_cost']['complete'])


if __name__ == '__main__':
    unittest.main()
