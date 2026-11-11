"""Read-only Codex telemetry. No model requests, estimates, or tokenizer guesses."""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
from .pricing import price_call, combine_costs, unavailable

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
          'reasoning_output_tokens', 'total_tokens')


def usage(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('Usage must be an object')
    details = value.get('input_tokens_details') or {}
    if not isinstance(details, dict):
        raise ValueError('Input token details must be an object')
    value = dict(value)
    value.setdefault('cached_input_tokens', details.get('cached_tokens', 0))
    value.setdefault('cache_write_input_tokens', details.get('cache_write_tokens', 0))
    return {key: max(0, int(value.get(key) or 0)) for key in FIELDS}


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def prompt_text(payload):
    parts = []
    for item in payload.get('content', []):
        text = item.get('text', '') if isinstance(item, dict) else ''
        # Context inserted by the app is not a question from the user.
        if not text or text.lstrip().startswith((
            '# AGENTS.md instructions', '<environment_context>',
            '<external_codex_apps_open_page>', '<permissions instructions>',
            '<turn_aborted>', '<user_instructions>', '<system-reminder>')):
            continue
        parts.append(text.strip())
    return '\n'.join(parts)


def normalize_limit(raw, observed_at, source):
    if not isinstance(raw, dict):
        raise ValueError('Quota must be an object')
    def window(data):
        if not data:
            return None
        if not isinstance(data, dict):
            raise ValueError('Quota window must be an object')
        percent = data.get('usedPercent', data.get('used_percent'))
        if percent is None:
            return None
        return {'used_percent': float(percent),
                'window_minutes': data.get('windowDurationMins', data.get('window_minutes')),
                'resets_at': data.get('resetsAt', data.get('resets_at'))}
    return {'id': raw.get('limitId', raw.get('limit_id')) or 'codex',
            'name': raw.get('limitName', raw.get('limit_name')) or 'Codex',
            'plan': raw.get('planType', raw.get('plan_type')),
            'source': source, 'observed_at': observed_at,
            'primary': window(raw.get('primary')), 'secondary': window(raw.get('secondary'))}


def weekly_cycle_start(limits, now):
    """Use the active standard Codex window, not the first observed 0% reading."""
    for limit in limits:
        if limit['id'] != 'codex':
            continue
        observed = timestamp(limit.get('observed_at'))
        for key in ('primary', 'secondary'):
            window = limit.get(key)
            if not window or window.get('window_minutes') != 7 * 24 * 60:
                continue
            reset = timestamp(window.get('resets_at'))
            if reset is None or observed is None:
                continue
            start = reset - 7 * 86400
            if start <= now < reset and start <= observed < reset:
                return start
    return None


class Rollout:
    """Incremental binary reader: incomplete final lines are retried, never consumed."""

    def __init__(self, path, thread_id=None):
        self.path = Path(path)
        self.thread_id = thread_id
        self.offset = 0
        self.identity = None
        self.legacy_total = usage()
        self.legacy_seen = False
        self.pending_usage = usage()
        self.modern_turn_usage = {}
        self.modern_turn_totals = {}
        self.root_usage = {}
        self.total = usage()
        self.raw_total = usage()
        self.turns = []
        self.by_id = {}
        self.current = None
        self.meta = {}
        self.limits = {}
        self.warnings = []
        self.bad_lines = 0
        self.responses = set()
        self.last_event_at = None
        self.last_usage_at = None
        self.modern_records = 0
        self.own_records = usage()
        self.orphan_costs = []
        self.root_costs = {}
        self.last_stat = None

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def start(self, turn_id, at, explicit=True):
        if turn_id and turn_id in self.by_id:
            self.current = self.by_id[turn_id]
            return self.current
        if self.current and self.current['status'] == 'active':
            if self.current['id'].startswith('legacy:') and explicit:
                old = self.current['id']
                self.current['id'] = turn_id or old
                self.by_id.pop(old, None)
                self.by_id[self.current['id']] = self.current
                return self.current
            self.current['status'] = 'unknown'
            self.current['ended_at'] = at
        turn = {'id': turn_id or f'legacy:{len(self.turns)+1}', 'prompt': '',
                'started_at': at, 'prompt_at': None, 'ended_at': None, 'status': 'active',
                'tokens': usage(), 'avg_tps': None, 'limit_deltas': [], '_legacy_observed': False,
                '_limit_start': dict(self.limits), 'root_turn_id': None,
                '_active_model': None, '_api_costs': [], '_models': []}
        self.turns.append(turn)
        self.by_id[turn['id']] = turn
        self.current = turn
        return turn

    def add_prompt(self, text, at):
        if not text:
            return
        turn = self.current
        if not turn or turn['status'] != 'active':
            turn = self.start(None, at, explicit=False)
        if turn['prompt_at'] is None:
            turn['prompt_at'] = at
        # Some versions write the same message as both response_item and event_msg.
        if text not in turn['prompt'].split('\n\n---\n\n'):
            turn['prompt'] = (turn['prompt'] + '\n\n---\n\n' + text).strip('\n- ')

    def count(self, value, at, turn_id=None, last=None):
        raw = usage(value)
        self.legacy_seen = True
        if raw == self.raw_total:
            return
        self.total = self.legacy_total
        self.pending_usage = usage()
        if raw['total_tokens'] < self.raw_total['total_tokens']:
            # A reset is not a new chat; retain the previous segment.
            self.warn('Usage counter reset detected; totals combine reported counter segments.')
            delta = usage(last) if last else raw
        else:
            delta = {k: max(0, raw[k] - self.raw_total[k]) for k in FIELDS}
        self.raw_total = raw
        for k in FIELDS:
            self.total[k] += delta[k]
        turn = self.by_id.get(turn_id) if turn_id else self.current
        if not turn:
            turn = self.start(turn_id, at)
            turn['status'] = 'unknown'
            self.warn('Some usage has no recorded question/turn boundary.')
        for k in FIELDS:
            turn['tokens'][k] += delta[k]
        turn['_legacy_observed'] = True
        self.last_usage_at = at

    def process(self, event):
        kind = event.get('type')
        payload = event.get('payload') or {}
        if not isinstance(payload, dict):
            return
        at = timestamp(event.get('timestamp'))
        self.last_event_at = at or self.last_event_at
        if kind == 'session_meta':
            self.meta.update(payload)
            if payload.get('forked_from_id'):
                self.warn('Forked history: inherited usage may be included; do not add to the source chat.')
        elif kind == 'turn_context':
            tid = payload.get('turn_id')
            if tid and (not self.current or self.current['id'] != tid):
                self.start(tid, at)
            self.meta['model'] = payload.get('model', self.meta.get('model'))
            if tid in self.by_id:
                self.by_id[tid]['_active_model'] = payload.get('model')
        elif kind == 'response_item':
            if payload.get('type') == 'message' and payload.get('role') == 'user':
                self.add_prompt(prompt_text(payload), at)
        elif kind == 'token_usage_record':
            values = usage(payload.get('usage'))
            response = (payload.get('thread_id'), payload.get('response_id')) if payload.get('response_id') else (event.get('ordinal'), event.get('timestamp'), json.dumps(payload.get('usage'),sort_keys=True))
            if response in self.responses:
                return
            if response:
                self.responses.add(response)
            self.modern_records += 1
            for k, v in values.items():
                self.own_records[k] += v
                self.pending_usage[k] += v
            tid = payload.get('turn_id')
            if tid and tid not in self.by_id:
                self.start(tid, at)
            if tid in self.by_id:
                turn = self.by_id[tid]
                turn['root_turn_id'] = payload.get('root_turn_id')
                model = payload.get('model') or turn['_active_model']
                if model not in turn['_models']:
                    turn['_models'].append(model)
                cost = price_call(model, payload.get('usage') or {})
                cost['models'] = [model or 'unknown']
                turn['_api_costs'].append(cost)
            else:
                cost = unavailable('Response has no recorded turn boundary/model.')
                self.orphan_costs.append(cost)
            root_id = payload.get('root_turn_id')
            if root_id and (not self.thread_id or payload.get('thread_id') in (None, self.thread_id)):
                root_usage = self.root_usage.setdefault(root_id, usage())
                for k, v in usage(payload.get('usage')).items():
                    root_usage[k] += v
                self.root_costs.setdefault(root_id, []).append(cost)
            turn_usage = self.modern_turn_usage.setdefault(tid, usage())
            for k, v in usage(payload.get('usage')).items():
                turn_usage[k] += v
            if payload.get('turn_token_usage') is not None:
                self.modern_turn_totals[tid] = usage(payload['turn_token_usage'])
            if self.legacy_seen:
                self.total = {k:self.legacy_total[k]+self.pending_usage[k] for k in FIELDS}
            else:
                self.total = dict(self.own_records)
            self.last_usage_at = at
        elif kind == 'event_msg':
            subtype = payload.get('type')
            tid = payload.get('turn_id')
            if subtype in ('task_started', 'turn_started'):
                turn = self.start(tid, timestamp(payload.get('started_at')) or at)
                turn['root_turn_id'] = payload.get('root_turn_id')
            elif subtype == 'user_message':
                self.add_prompt(payload.get('message') or '', at)
            elif subtype in ('task_complete', 'turn_complete', 'turn_aborted', 'task_failed'):
                turn = self.by_id.get(tid) if tid else self.current
                if turn:
                    turn['ended_at'] = timestamp(payload.get('completed_at')) or at
                    turn['status'] = 'completed' if subtype in ('task_complete', 'turn_complete') else 'interrupted'
                    turn['_limit_end'] = dict(self.limits)
            elif subtype == 'token_count':
                raw_limit = payload.get('rate_limits')
                if raw_limit:
                    limit = normalize_limit(raw_limit, at, 'rollout')
                    self.limits[limit['id']] = limit
                info = payload.get('info') or {}
                if info.get('total_token_usage') is not None:
                    self.count(info['total_token_usage'], at, last=info.get('last_token_usage'))
        # Compaction does not reset the cumulative usage or constitute a user turn.

    def refresh(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                path = self.path
                self.__init__(path, self.thread_id)
                self.warn('Rollout replaced/truncated; rebuilt from the available history.')
            self.identity = identity
            if self.last_stat == (stat.st_size, stat.st_mtime_ns):
                return
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        break
                    self.offset = stream.tell()
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            self.process(event)
                    except (ValueError, TypeError, OverflowError, AttributeError):
                        self.bad_lines += 1
            self.last_stat = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            self.warn('Rollout is unavailable; showing only previously read telemetry.')
        if self.bad_lines:
            self.warn(f'Skipped malformed records: {self.bad_lines}. History may be incomplete.')

    def public_turns(self, now, include=None):
        result = []
        for turn in self.turns:
            if include is not None and not include(turn):
                continue
            item = {k: v for k, v in turn.items() if not k.startswith('_')}
            # The newer format records actual response usage with explicit turn IDs.
            # Its cumulative base can differ from the older chat counter.
            if turn['id'] in self.modern_turn_totals:
                item['tokens'] = dict(self.modern_turn_totals[turn['id']])
                item['usage_source'] = 'recorded turn usage'
            elif turn['id'] in self.modern_turn_usage:
                item['tokens'] = dict(self.modern_turn_usage[turn['id']])
                item['usage_source'] = 'sum of unique response usage'
            else:
                item['usage_source'] = 'legacy counter delta'
            elapsed = (turn['ended_at'] or now) - (turn['started_at'] or now)
            item['avg_tps'] = item['tokens']['output_tokens'] / elapsed if elapsed > 0 else None
            item['avg_tps_basis'] = 'output tokens / elapsed turn time (includes tools and waiting)'
            item['models'] = [m or 'unknown' for m in turn['_models']] or [turn['_active_model'] or 'unknown']
            item['model'] = ' + '.join(item['models'])
            item['api_cost'] = combine_costs(turn['_api_costs'])
            recorded = self.modern_turn_usage.get(turn['id'])
            if item['tokens']['total_tokens'] and (not recorded or any(recorded[k] != item['tokens'][k] for k in FIELDS)):
                item['api_cost'] = combine_costs([item['api_cost'], unavailable(
                    'Per-response coverage is incomplete; aggregate counters cannot determine the per-call price tier.')])
            if self.bad_lines:
                item['api_cost'] = combine_costs([item['api_cost'], unavailable(
                    'Malformed telemetry was skipped; the known cost may be incomplete.')])
            item['prompt'] = turn['prompt'] or '(No user question recorded: automatic/internal continuation)'
            changes = []
            for key, end in self.limits.items():
                start = turn['_limit_start'].get(key)
                # End-of-turn samples are captured below by monitor-independent replay.
                end = turn.get('_limit_end', {}).get(key, end) if turn['status'] != 'active' else end
                if not start:
                    continue
                for label in ('primary', 'secondary'):
                    a, b = start.get(label), end.get(label)
                    if a and b and a['resets_at'] == b['resets_at']:
                        delta = b['used_percent'] - a['used_percent']
                        if delta >= 0:
                            changes.append(f'{key}/{label}: +{delta:.2f} pp (account-wide observed delta)')
            item['limit_deltas'] = changes
            result.append(item)
        return result


class Monitor:
    def __init__(self, codex_home=None, limits_client=None):
        self.home = Path(codex_home or os.environ.get('CODEX_HOME', Path.home()/'.codex')).expanduser().resolve()
        self.limits_client = limits_client
        self.parsers = {}
        self.samples = {}
        self.metadata = []
        self.warnings = []
        self.limit_cache = {}
        self.fallback_scanned = False
        self.weekly_history = None

    def close(self):
        if self.limits_client:
            self.limits_client.close()

    def read_threads(self):
        paths = sorted(self.home.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else -1, reverse=True)
        self.warnings = []
        for db in paths:
            try:
                with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=.2)) as conn:
                    conn.execute('PRAGMA query_only=ON')
                    conn.row_factory = sqlite3.Row
                    raw = [dict(r) for r in conn.execute('SELECT * FROM threads ORDER BY updated_at DESC')]
                    edges = {}
                    try:
                        edges = dict(conn.execute('SELECT child_thread_id,parent_thread_id FROM thread_spawn_edges'))
                    except sqlite3.Error:
                        pass
                result = []
                for row in raw:
                    source = row.get('source', '')
                    parent = edges.get(row['id'])
                    try:
                        spawn = json.loads(source).get('subagent', {}).get('thread_spawn', {})
                        parent = parent or spawn.get('parent_thread_id')
                    except (ValueError, AttributeError, TypeError):
                        pass
                    result.append({'id': row['id'],
                        'title': row.get('name') or row.get('title') or row.get('agent_nickname') or row['id'],
                        'model': row.get('model') or '', 'cwd': row.get('cwd') or '',
                        'archived': bool(row.get('archived')), 'is_agent': bool(parent or row.get('thread_source') == 'subagent'),
                        'parent_id': parent, 'tokens': usage({'total_tokens':row.get('tokens_used', 0)}),
                        'updated_at': float(row.get('updated_at') or 0),
                        'rollout_path': row['rollout_path'], 'active': False})
                self.metadata = result
                return result
            except (sqlite3.Error, OSError, ValueError):
                continue
        self.warnings.append('Codex state database unavailable: discovering local rollout files; titles may use IDs.')
        result = []
        for folder in ('sessions', 'archived_sessions'):
            for path in (self.home/folder).rglob('*.jsonl'):
                try:
                    with path.open('rb') as stream:
                        meta = json.loads(stream.readline()).get('payload', {})
                    tid = meta.get('id', path.stem)
                    result.append({'id':tid, 'title':tid, 'model':'', 'cwd':meta.get('cwd',''),
                        'archived':folder == 'archived_sessions', 'is_agent':isinstance(meta.get('source'),dict),
                        'parent_id':None, 'tokens':usage(), 'updated_at':path.stat().st_mtime,
                        'rollout_path':str(path), 'active':False})
                except (OSError, ValueError):
                    continue
        self.metadata = sorted(result, key=lambda r:r['updated_at'], reverse=True)
        return self.metadata

    def parser(self, row):
        key = row['id']
        if key not in self.parsers or self.parsers[key].path != Path(row['rollout_path']):
            self.parsers[key] = Rollout(row['rollout_path'], key)
        self.parsers[key].refresh()
        return self.parsers[key]

    def period_snapshot(self, start, end):
        """Group turns in [start, end); None starts at the current weekly boundary."""
        now = time.time()
        rows = {row['id']: row for row in self.read_threads()}
        if start is None:
            start = weekly_cycle_start(self.recent_limits(list(rows.values())), end)
            if start is None:
                warning = 'Current Codex weekly reset time is unavailable.'
                return {'updated_at':now, 'start':None, 'end':end, 'turns':[], 'tokens':None,
                        'api_cost':unavailable(warning), 'warnings':[warning, *self.warnings]}
        warnings = list(self.warnings)
        gaps = set()
        by_chat = {}
        seen_turn_ids = set()

        def candidate(row):
            updated = row['updated_at']
            try:
                return not updated or max(updated, Path(row['rollout_path']).stat().st_mtime) >= start
            except OSError:
                return not updated or updated >= start

        def in_period(turn):
            at = turn['prompt_at'] if turn['prompt_at'] is not None else turn['started_at']
            return bool(turn['prompt'] and at is not None and start <= at < end)

        roots = [(row, self.parser(row)) for row in rows.values()
                 if not row['is_agent'] and candidate(row)]
        roots.sort(key=lambda pair: bool(pair[1].meta.get('forked_from_id')))
        for row, parser in roots:
            forked = bool(parser.meta.get('forked_from_id'))
            fork_at = timestamp(parser.meta.get('timestamp')) if forked else None
            if parser.last_stat is None or parser.bad_lines or (forked and fork_at is None):
                gaps.add(row['id'])
            if forked and fork_at is None:
                continue
            for turn in parser.public_turns(now, include=in_period):
                at = turn['prompt_at'] if turn['prompt_at'] is not None else turn['started_at']
                explicit = not turn['id'].startswith('legacy:')
                if (fork_at is not None and at < fork_at) or (explicit and turn['id'] in seen_turn_ids):
                    continue
                if explicit:
                    seen_turn_ids.add(turn['id'])
                turn.update(thread_id=row['id'], chat_title=row['title'], prompt_at=at,
                            children_tokens=usage(), period_child_costs=[])
                by_chat.setdefault(row['id'], {})[turn['id']] = turn

        for row in rows.values():
            if not row['is_agent'] or not candidate(row):
                continue
            ancestor = row
            visited = {row['id']}
            while ancestor.get('parent_id') in rows and ancestor['parent_id'] not in visited:
                ancestor = rows[ancestor['parent_id']]
                visited.add(ancestor['id'])
            turns = by_chat.get(ancestor['id'])
            if not turns:
                if ancestor['is_agent'] and row['tokens']['total_tokens']:
                    gaps.add(row['id'])
                continue
            parser = self.parser(row)
            linked = parser.root_usage
            if (parser.last_stat is None or parser.bad_lines
                    or sum(t['total_tokens'] for t in linked.values()) != parser.own_records['total_tokens']
                    or (parser.legacy_seen and not parser.modern_records and parser.total['total_tokens'])):
                gaps.add(row['id'])
            for turn_id in linked.keys() & turns.keys():
                turn = turns[turn_id]
                for key, value in linked[turn_id].items():
                    turn['children_tokens'][key] += value
                turn['period_child_costs'].extend(parser.root_costs[turn_id])

        turns = sorted((t for chat in by_chat.values() for t in chat.values()),
                       key=lambda t: (t['prompt_at'], t['thread_id'], t['id']))
        totals = usage()
        costs = []
        for turn in turns:
            turn['children_api_cost'] = combine_costs(turn.pop('period_child_costs'))
            turn['api_cost_with_children'] = combine_costs([turn['api_cost'], turn['children_api_cost']])
            turn['period_tokens'] = {k: turn['tokens'][k] + turn['children_tokens'][k] for k in FIELDS}
            for k, value in turn['period_tokens'].items():
                totals[k] += value
            costs.append(turn['api_cost_with_children'])
        if gaps:
            warning = f'{len(gaps)} candidate rollouts missing, malformed or unlinked; period coverage may be incomplete.'
            warnings.append(warning)
            costs.append(unavailable(warning))
        return {'updated_at':time.time(), 'warnings':warnings, 'turns':turns,
                'start':start, 'end':end, 'tokens':totals, 'api_cost':combine_costs(costs)}

    def weekly_periods(self):
        from .weekly import WeeklyHistory
        if self.weekly_history is None:
            self.weekly_history = WeeklyHistory()
        rows = self.read_threads()
        limits = self.recent_limits(rows)
        periods, warnings = self.weekly_history.periods(
            [row['rollout_path'] for row in rows], limits, time.time())
        return {'updated_at':time.time(), 'periods':periods, 'warnings':[*self.warnings, *warnings]}

    def weekly_snapshot(self, reset=None):
        from .weekly import RESET_TOLERANCE
        history = self.weekly_periods()
        selected = next((period for period in history['periods']
                         if (period['current'] if reset is None else abs(period['reset_at'] - reset) <= RESET_TOLERANCE)), None)
        if selected is None:
            warning = 'The selected weekly period is unavailable in local reset history.'
            return {'updated_at':time.time(), 'start':None, 'end':time.time(), 'turns':[], 'tokens':None,
                    'api_cost':unavailable(warning), 'warnings':[warning],
                    'weekly_periods':history['periods']}
        snapshot = self.period_snapshot(selected['start'], selected['end'])
        snapshot.update(weekly_period=selected, weekly_periods=history['periods'])
        snapshot['warnings'].extend(history['warnings'])
        return snapshot

    def recent_limits(self, rows):
        # Read only bounded tails of recently updated rollouts; never rescan whole archives.
        for row in rows[:12]:
            path = Path(row['rollout_path'])
            try:
                stat = path.stat()
                key = (str(path), stat.st_size, stat.st_mtime_ns)
                if getattr(self, '_tail_keys', {}).get(str(path)) == key:
                    continue
                if not hasattr(self, '_tail_keys'):
                    self._tail_keys = {}
                self._tail_keys[str(path)] = key
                with path.open('rb') as stream:
                    start = max(0, stat.st_size-512*1024)
                    stream.seek(start)
                    if start:
                        stream.readline()
                    lines = stream.read().splitlines(keepends=True)
                for line in reversed(lines):
                    if not line.endswith(b'\n') or b'"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        raw = event.get('payload',{}).get('rate_limits')
                        if raw:
                            limit = normalize_limit(raw, timestamp(event.get('timestamp')), 'rollout')
                            existing = self.limit_cache.get(limit['id'])
                            if not existing or (limit['observed_at'] or 0) > (existing['observed_at'] or 0):
                                self.limit_cache[limit['id']] = limit
                    except (ValueError, AttributeError):
                        continue
            except OSError:
                continue
        if self.limits_client:
            limits, error = self.limits_client.snapshot()
            for limit in limits:
                previous = self.limit_cache.get(limit['id'])
                if not previous or (limit['observed_at'] or 0) >= (previous['observed_at'] or 0):
                    self.limit_cache[limit['id']] = limit
            if error:
                self.warnings.append(error)
        return sorted(self.limit_cache.values(), key=lambda x:x['id'])

    def snapshot(self, selected_id=None, include_agents=False, query=''):
        now = time.time()
        rows = self.read_threads()
        limits = self.recent_limits(rows)
        visible = [dict(r) for r in rows if (include_agents or not r['is_agent']) and query.casefold() in (r['title']+' '+r['id']).casefold()]
        selected_row = next((r for r in visible if r['id'] == selected_id), None)
        if not selected_row and visible:
            selected_row = visible[0]
        selected = None
        if selected_row:
            parser = self.parser(selected_row)
            selected = dict(selected_row)
            selected['tokens'] = dict(parser.total)
            selected['db_total'] = selected_row['tokens']['total_tokens']
            selected['turns'] = parser.public_turns(now)
            selected['recorded_response_tokens'] = dict(parser.own_records)
            selected['turns_total'] = sum(t['tokens']['total_tokens'] for t in selected['turns'])
            selected['active'] = bool(parser.current and parser.current['status'] == 'active')
            selected['warnings'] = list(parser.warnings)
            if selected['turns_total'] != parser.total['total_tokens']:
                selected['warnings'].append('Historical chat counter differs from summed turn usage; response and legacy formats are shown separately.')
            if selected['db_total'] and not parser.total['total_tokens']:
                selected['tokens']['total_tokens'] = selected['db_total']
                selected['warnings'].append('Only database total is available; per-turn breakdown is unavailable.')
            elif selected['db_total'] != parser.total['total_tokens'] and not parser.pending_usage['total_tokens']:
                selected['warnings'].append('Database and rollout totals differ (live update or incomplete history); showing recorded rollout total.')
            if parser.meta.get('forked_from_id'):
                selected['coverage'] = 'Available fork history; inherited records may be included.'
            else:
                selected['coverage'] = 'Recorded model usage: context, tools and repeated input included. Cached input is part of input; reasoning is part of output.'
            key = selected['id']
            previous = self.samples.get(key)
            tick = time.monotonic()
            selected['tps'] = {'total':None, 'output':None, 'interval':0}
            if previous:
                elapsed = tick - previous[0]
                if elapsed > 0:
                    selected['tps'] = {'total':max(0,parser.total['total_tokens']-previous[1])/elapsed,
                        'output':max(0,parser.total['output_tokens']-previous[2])/elapsed, 'interval':elapsed}
            self.samples[key] = (tick, parser.total['total_tokens'], parser.total['output_tokens'])
            # Keep child streams separate: a parent counter is not evidence of child usage.
            descendants = set()
            frontier = {selected['id']}
            while frontier:
                found = {r['id'] for r in rows if r['parent_id'] in frontier} - descendants
                descendants |= found
                frontier = found
            selected['children_total'] = sum(r['tokens']['total_tokens'] for r in rows if r['id'] in descendants)
            selected['children_count'] = len(descendants)
            children_by_turn = {}
            child_costs_by_turn = {}
            child_costs = []
            unlinked_child_usage = False
            selected['children_recorded_tokens'] = usage()
            for row in rows:
                if row['id'] not in descendants:
                    continue
                child = self.parser(row)
                child_turns = child.public_turns(now)
                child_costs.extend(t['api_cost'] for t in child_turns)
                child_costs.extend(child.orphan_costs)
                if not child_turns and row['tokens']['total_tokens']:
                    child_costs.append(unavailable('Descendant rollout/response history is unavailable.'))
                    unlinked_child_usage = True
                if sum(v['total_tokens'] for v in child.root_usage.values()) != child.own_records['total_tokens'] or (child.legacy_seen and not child.modern_records and child.total['total_tokens']):
                    unlinked_child_usage = True
                for k, value in child.own_records.items():
                    selected['children_recorded_tokens'][k] += value
                for root_id, values in child.root_usage.items():
                    target = children_by_turn.setdefault(root_id, usage())
                    for k, value in values.items():
                        target[k] += value
                for root_id, costs in child.root_costs.items():
                    child_costs_by_turn.setdefault(root_id, []).extend(costs)
            for turn in selected['turns']:
                turn['children_tokens'] = children_by_turn.get(turn['id'], usage())
                turn['children_api_cost'] = combine_costs(child_costs_by_turn.get(turn['id'], []))
                if unlinked_child_usage:
                    turn['children_api_cost'] = combine_costs([turn['children_api_cost'], unavailable(
                        'Some descendant usage has no root-turn link; per-turn agent price is incomplete.')])
                turn['api_cost_with_children'] = combine_costs([turn['api_cost'], turn['children_api_cost']])
                turn['children_usage_source'] = 'unique descendant response records with matching root_turn_id; older unlinked usage is excluded'
            selected['api_cost'] = combine_costs([t['api_cost'] for t in selected['turns']] + parser.orphan_costs)
            if not selected['turns'] and selected['tokens']['total_tokens']:
                selected['api_cost'] = unavailable('Rollout/response history is unavailable; database total cannot determine API cost.')
            selected['children_api_cost'] = combine_costs(child_costs)
            selected['api_cost_with_children'] = combine_costs([selected['api_cost'], selected['children_api_cost']])
            for row in visible:
                if row['id'] == key:
                    row['tokens'] = selected['tokens']
                    row['active'] = selected['active']
        return {'updated_at':time.time(), 'threads':visible, 'selected':selected,
                'limits':limits, 'warnings':self.warnings, 'refresh_seconds':5}
