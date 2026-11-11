from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import curses

from codex_token_monitor.period_ui import PeriodView, parse_minute
from codex_token_monitor.telemetry import Monitor, normalize_limit
from codex_token_monitor.ui import _Dashboard


def record(kind, at, **payload):
    return dict(type=kind, timestamp=at, payload=payload)


def response(at, thread, turn, root=None, model='gpt-6.1-sol'):
    return record('token_usage_record', at, thread_id=thread, turn_id=turn,
                  root_turn_id=root or turn, response_id=f'response-{turn}', model=model,
                  usage=dict(input_tokens=90, cached_input_tokens=80, output_tokens=10, total_tokens=100))


def question(start, at, turn, text):
    return [record('event_msg', start, type='task_started', turn_id=turn),
            record('event_msg', at, type='user_message', message=text)]


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = self.home / 'state_5.sqlite'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE threads(id,title,rollout_path,updated_at,tokens_used,thread_source)')
            db.execute('CREATE TABLE thread_spawn_edges(child_thread_id,parent_thread_id)')
        self.monitor = Monitor(self.home)
        self.addCleanup(self.monitor.close)

    def add_thread(self, name, events, parent=None, missing=False):
        path = self.home / f'{name}.jsonl'
        if not missing:
            path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        with sqlite3.connect(self.db) as db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                       (name, f'Chat {name}', str(path), 180, 100, 'subagent' if parent else 'user'))
            if parent:
                db.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (name, parent))

    def test_chronology_prompt_boundaries_and_child_costs(self):
        # The first task starts earlier than the filter, but its prompt is at the boundary.
        self.add_thread('a', [*question(98, 100, 'one', 'At the start'),
            response(110, 'a', 'one'),
            record('event_msg', 160, type='task_complete', turn_id='one'),
            *question(170, 170, 'outside', 'After the range'), response(180, 'a', 'outside')])
        self.add_thread('b', [*question(120, 120, 'two', 'Later'), response(122, 'b', 'two'),
            *question(149, 150, 'end', 'At the exclusive end'), response(151, 'b', 'end')])
        child_response = response(158, 'agent', 'child', 'one', 'gpt-6-luna')
        self.add_thread('agent', [child_response, child_response], parent='a')
        report = self.monitor.period_snapshot(100, 150)
        self.assertEqual([(t['prompt_at'], t['chat_title'], t['prompt']) for t in report['turns']],
                         [(100, 'Chat a', 'At the start'), (120, 'Chat b', 'Later')])
        self.assertEqual(report['turns'][0]['period_tokens']['total_tokens'], 200)
        self.assertEqual(report['tokens']['total_tokens'], 300)
        self.assertEqual(report['tokens']['cached_input_tokens'], 240)
        self.assertEqual(report['api_cost']['calls'], 3)
        # Sol: $0.000128 each, Luna: $0.0000068.
        self.assertEqual(Decimal(report['api_cost']['usd']), Decimal('.0002628'))

    def test_fork_history_is_excluded_and_unavailable_rollout_is_partial(self):
        original = [*question(100, 101, 'original', 'Original'), response(105, 'source', 'original')]
        self.add_thread('source', original)
        self.add_thread('fork', [record('session_meta', 130, forked_from_id='source', timestamp=130),
            *original, *question(135, 136, 'new', 'New'), response(140, 'fork', 'new')])
        self.add_thread('missing', [], missing=True)
        report = self.monitor.period_snapshot(100, 150)
        self.assertEqual([t['thread_id'] for t in report['turns']], ['source', 'fork'])
        self.assertEqual(report['tokens']['total_tokens'], 200)
        self.assertFalse(report['api_cost']['complete'])
        self.assertEqual(Decimal(report['api_cost']['known_usd']), Decimal('.000256'))
        self.assertTrue(report['warnings'])

    def test_period_controls_and_custom_range(self):
        dashboard = _Dashboard(None, self.monitor, 'a', 5, 2)
        now = datetime(2026, 5, 6, 13, 14).timestamp()
        dashboard.key('1')
        self.assertEqual(dashboard.period_bounds(dashboard.request_key(), now),
                         (datetime(2026, 5, 6).timestamp(), now))
        dashboard.key('2')
        self.assertEqual(PeriodView.period_bounds(dashboard.request_key(), now), (now - 86400, now))
        dashboard.key('6')
        for key in '2026-05-04 10:15\t2026-05-05 11:30\n':
            dashboard.key(key)
        self.assertEqual(dashboard.period_bounds(dashboard.request_key(), now),
                         (parse_minute('2026-05-04 10:15'), parse_minute('2026-05-05 11:30')))
        dashboard.key('c')
        self.assertEqual((dashboard.mode, dashboard.selected), ('chat', 'a'))

    def test_custom_enter_advances_and_blank_end_keeps_tracking_now(self):
        dashboard = _Dashboard(None, self.monitor, 'a', 5, 2)
        dashboard.key('6')
        for key in '2026-05-04 10:15\n':
            dashboard.key(key)
        self.assertEqual((dashboard.range_field, dashboard.range_error), (1, ''))
        with patch('codex_token_monitor.period_ui.time.time', return_value=parse_minute('2026-05-05 11:30')):
            dashboard.key('\n')
        start = parse_minute('2026-05-04 10:15')
        self.assertEqual(dashboard.custom_period, (start, None))
        self.assertEqual(dashboard.period_bounds(dashboard.request_key(), start + 900), (start, start + 900))
        dashboard.key('6')
        self.assertEqual(dashboard.range_fields, ['2026-05-04 10:15', ''])
        with patch('curses.getmouse', return_value=(0, 22, 7, 0, curses.BUTTON1_CLICKED)):
            dashboard.key(curses.KEY_MOUSE)
        self.assertEqual(dashboard.range_field, 1)

    def test_weekly_preset_uses_current_reset_boundary_and_ignores_other_buckets(self):
        start, end = 100, 150
        weekly = {'windowDurationMins':10080, 'resetsAt':start + 7 * 86400, 'usedPercent':14}
        quota = normalize_limit({'limitId':'codex', 'secondary':weekly}, 140, 'test')
        other = normalize_limit({'limitId':'other', 'primary':dict(weekly, resetsAt=weekly['resetsAt'] + 25)}, 140, 'test')
        self.add_thread('a', [*question(98, 99, 'before', 'Before reset'),
            response(99, 'a', 'before'), *question(100, 100, 'inside', 'After reset'),
            response(101, 'a', 'inside')])
        with patch.object(self.monitor, 'recent_limits', return_value=[other, quota]):
            report = self.monitor.period_snapshot(None, end)
            self.assertEqual(report['start'], start)
            self.assertEqual([turn['id'] for turn in report['turns']], ['inside'])
            self.assertEqual(report['tokens']['total_tokens'], 100)
            expired = self.monitor.period_snapshot(None, weekly['resetsAt'])
            self.assertIsNone(expired['start'])
            self.assertIsNone(expired['tokens'])

    def test_extra_presets_and_wrapped_header(self):
        dashboard = _Dashboard(None, self.monitor, 'a', 5, 2)
        now = 40 * 86400
        for key, days in [('3', 7), ('4', 30)]:
            dashboard.key(key)
            self.assertEqual(dashboard.period_bounds(dashboard.request_key(), now), (now - days * 86400, now))
        dashboard.key('5')
        self.assertEqual(dashboard.period_bounds(dashboard.request_key(), now), (None, now))
        tabs = list(dashboard.view_tabs(58))
        self.assertEqual(tabs[-1][0], 3)
        self.assertTrue(next(tab[-1] for tab in tabs if tab[2] == '5'))


if __name__ == '__main__':
    unittest.main()
