"""Dependency-free terminal dashboard for locally reported Codex token usage."""
from __future__ import annotations
import curses
import queue
import threading
import math
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Any
from .period_ui import PERIOD_LABELS, PeriodView

def _clean(value: Any) -> str:
    """Prevent data from becoming terminal controls, including bidi controls."""
    return ''.join((ch if not unicodedata.category(ch).startswith('C') else ' ' for ch in str(value if value is not None else '')))

def _width(text: str) -> int:
    return sum((0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text))

def _clip(value: Any, width: int) -> str:
    text = _clean(value)
    if width <= 0:
        return ''
    if _width(text) <= width:
        return text
    result, used = ([], 0)
    for char in text:
        size = _width(char)
        if used + size > width - 1:
            break
        result.append(char)
        used += size
    return ''.join(result) + '…'

def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError, OverflowError):
        return 'N/A'

def _rate(value: Any) -> str:
    try:
        number = float(value)
        return f"{number:,.2f}" if math.isfinite(number) else 'N/A'
    except (ValueError, TypeError):
        return 'N/A'

def _money(cost: Any) -> str:
    if not cost:
        return 'N/A'
    try:
        amount = Decimal(cost.get('usd') if cost.get('complete') else cost.get('known_usd', '0'))
        if not amount.is_finite():
            return 'N/A'
        if not cost.get('complete') and (not amount):
            return 'N/A'
        return ('' if cost.get('complete') else '>=') + f"${amount:.6f}"
    except (ValueError, TypeError, InvalidOperation):
        return 'N/A'

def _cost_breakdown(cost: Any) -> list[str]:
    """Use the same scope and completeness as the displayed total."""
    cost = cost or {}
    parts = cost.get('components_usd') or {}
    def component(key):
        if key not in parts:
            return 'N/A'
        return _money({'complete': cost.get('complete', False),
                       'usd': parts[key], 'known_usd': parts[key]})
    return [f"USD: uncached {component('input')} · cache read {component('cached_input')}",
            f"USD: cache write {component('cache_write')} · output {component('output')}"]

def _date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime('%d %b %H:%M')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'N/A'

def _age(value: Any) -> str:
    try:
        elapsed = max(0, int(time.time() - float(value)))
    except (ValueError, TypeError, OverflowError):
        return 'unknown age'
    if elapsed < 60:
        return f"{elapsed}s ago"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"

def _wrap(value: Any, width: int) -> list[str]:
    words = _clean(value).split()
    lines: list[str] = []
    current = ''
    for word in words:
        if current and _width(current + ' ' + word) > width:
            lines.append(current)
            current = ''
        while _width(word) > width and width > 1:
            fragment = _clip(word, width)[:-1]
            if not fragment:
                break
            lines.append(fragment)
            word = word[len(fragment):]
        current = (current + ' ' + word).strip()
    if current:
        lines.append(current)
    return lines or ['(No prompt recorded)']

class _Dashboard(PeriodView):

    def __init__(self, screen: Any, monitor: Any, selected: str | None, refresh: float, decimals: int):
        self.screen = screen
        self.monitor = monitor
        self.selected = selected
        self.refresh = max(0.2, float(refresh))
        self.decimals = 3 if decimals == 3 else 2
        self.data: dict[str, Any] = {}
        self.threads: list[dict[str, Any]] = []
        self.focus = 'chats'
        self.include_agents = False
        self.query = ''
        self.searching = False
        self.search_before = ''
        self.follow = selected is None
        self.turn_index = 0
        self.turn_id: str | None = None
        self.chat_offset = 0
        self.turn_offset = 0
        self.detail = False
        self.detail_offset = 0
        self.next_refresh = 0.0
        self.error = ''
        self.chat_area = (0, 0, 0, 0)
        self.turn_area = (0, 0, 0, 0)
        self.colors = False
        self.limits_view = False
        self._results = queue.Queue()
        self._worker = None
        self._loading = False
        self._view_cache = {}
        self.init_period()

    def put(self, y: int, x: int, text: Any, attr: int=0, width: int | None=None) -> None:
        height, columns = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or (x >= columns):
            return
        available = max(0, columns - x - (1 if y == height - 1 else 0))
        try:
            self.screen.addstr(y, x, _clip(text, min(available, available if width is None else width)), attr)
        except curses.error:
            pass

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if self.colors else 0

    def setup(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        # Keep bare Escape separate from the wait for terminal function-key bytes.
        if hasattr(curses, 'set_escdelay'):
            curses.set_escdelay(20)
        self.screen.timeout(25)
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                for pair, foreground in ((1, curses.COLOR_CYAN), (2, curses.COLOR_GREEN), (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED)):
                    curses.init_pair(pair, foreground, -1)
                self.colors = True
            except curses.error:
                pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass

    def request_key(self):
        if self.weekly_picker:
            return ('weekly_picker',)
        if self.mode == 'time':
            return ('time', self.period_preset,
                    self.weekly_reset if self.period_preset == 'weekly' else self.custom_period)
        return (None if self.follow else self.selected, self.include_agents, self.query, self.follow)

    def fetch(self) -> None:
        if self._worker is not None:
            return
        request = self.request_key()
        is_time = self.mode == 'time'
        self._loading = True
        self.next_refresh = time.monotonic() + self.refresh
        def load():
            try:
                if request[0] == 'weekly_picker':
                    data = self.monitor.weekly_periods()
                elif is_time and request[1] == 'weekly':
                    data = self.monitor.weekly_snapshot(request[2])
                elif is_time:
                    data = self.monitor.period_snapshot(*self.period_bounds(request, time.time()))
                else:
                    data = self.monitor.snapshot(selected_id=request[0], include_agents=request[1], query=request[2])
                self._results.put((request, data, None))
            except Exception as exc:
                self._results.put((request, None, str(exc)))
        self._worker = threading.Thread(target=load, name='codex-monitor-reader', daemon=True)
        self._worker.start()

    def poll_results(self) -> None:
        try:
            request, data, error = self._results.get_nowait()
        except queue.Empty:
            return
        self._worker = None
        self._loading = False
        if data and data.get('selected'):
            key = data['selected']['id']
            self._view_cache.pop(key, None)
            self._view_cache[key] = data
            while len(self._view_cache) > 8:
                self._view_cache.pop(next(iter(self._view_cache)))
        if request != self.request_key():
            self.next_refresh = 0
            return
        if error:
            self.error = 'Refresh failed: ' + _clean(error)
            return
        if self.weekly_picker:
            previous = (self.weekly_periods[self.weekly_index]['reset_at']
                        if self.weekly_periods else self.weekly_reset)
            self.weekly_periods = data['periods']
            self.weekly_index = next((i for i, p in enumerate(self.weekly_periods)
                                      if p['reset_at'] == previous), 0)
            self.weekly_picker_warnings = data['warnings']
            self.error = ''
            self.next_refresh = time.monotonic() + self.refresh
            return
        if self.mode == 'chat':
            self.threads = list(data.get('threads') or [])
            returned = data.get('selected') or {}
            if returned.get('id') != self.selected:
                self.selected = returned.get('id')
                self.turn_index = self.turn_offset = 0
                self.turn_id = None
                self.detail = False
        self.data = data
        turns = self.turns()
        if self.turn_id is not None:
            index = next((i for i, turn in enumerate(turns) if self.turn_key(turn) == self.turn_id), None)
            if index is None:
                self.detail = False
            else:
                self.turn_index = index
        self.turn_index = min(self.turn_index, max(0, len(turns)-1))
        if turns:
            self.turn_id = self.turn_key(turns[self.turn_index])
        self.error = ''
        self.next_refresh = time.monotonic() + self.refresh

    def turns(self) -> list[dict[str, Any]]:
        if self.mode == 'time':
            return self.data.get('turns') or []
        return list(reversed((self.data.get('selected') or {}).get('turns') or []))

    def turn_key(self, turn):
        return (turn['thread_id'], turn['id']) if self.mode == 'time' else turn.get('id')

    def draw(self, loading: bool=False) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        accent = self.color(1) | curses.A_BOLD
        self.put(0, 1, 'CODEX / TOKEN MONITOR', accent)
        view = ('FOLLOW latest' if self.follow else 'MANUAL selection') if self.mode == 'chat' else 'TIME range'
        self.put(1, 1, f"Local usage · every {self.refresh:g}s · {view} · {('Loading…' if loading else _age(self.data.get('updated_at')))}", curses.A_DIM)
        top = 3
        for y, x, _, tab, selected in self.view_tabs(width):
            self.put(y, x, tab, curses.A_REVERSE if selected else accent)
            top = y + 1
        if height < 15 or width < 58:
            self.put(4, 2, 'Please enlarge the terminal (58 columns × 15 rows).', accent)
            self.put(6, 2, 'Tab panel · q quit · r refresh')
            self.screen.refresh()
            return
        if self.range_fields is not None:
            self.draw_range_editor()
            self.screen.refresh()
            return
        if self.weekly_picker:
            self.draw_weekly_picker(top, height - 3)
            self.screen.refresh()
            return
        if self.mode == 'time':
            self.draw_period(height, width, top)
            self.screen.refresh()
            return
        split = width >= 100
        show_chats = split or self.focus == 'chats'
        left_width = min(37, max(26, width // 3)) if split else width
        main_x = left_width + 1 if split else 0
        main_width = width - main_x
        bottom = height - 3
        if show_chats:
            self.draw_chats(top, bottom, left_width)
        if split:
            for row in range(top, bottom):
                self.put(row, left_width, '│', curses.A_DIM)
        if split or not show_chats:
            self.draw_main(top, bottom, main_x, main_width)
        selected = self.data.get('selected') or {}
        warnings = list(self.data.get('warnings') or []) + list(selected.get('warnings') or [])
        if self.error:
            warnings.insert(0, self.error)
        status = ' | '.join((_clean(item) for item in warnings)) if warnings else _clean(selected.get('coverage') or self.data.get('coverage') or 'Waiting for local usage records')
        self.put(height - 3, 1, status, self.color(3) if warnings else curses.A_DIM)
        controls = 'Tab panel  ↑↓/jk select  Enter detail  / search  f follow'
        controls += '  a agents'
        self.put(height - 2, 1, '/ ' + self.query + '▏' if self.searching else controls, accent if self.searching else curses.A_DIM)
        help_text = 'q quit  r refresh  Esc back'
        help_text += f"  l limits  d decimals ({self.decimals}) · rates = reported batches, not decode speed"
        self.put(height - 1, 1, help_text, curses.A_DIM)
        self.screen.refresh()

    def draw_period(self, height, width, top=3):
        label = PERIOD_LABELS[self.period_preset]
        if self.period_preset == 'weekly':
            selected = self.data.get('weekly_period')
            if selected:
                label = 'Current weekly period' if selected['current'] else 'Previous weekly period'
        if self.data.get('start') is not None:
            label += f" · {self.local_minute(self.data['start'])} → {self.local_minute(self.data['end'])} (local)"
        self.put(top, 1, label, self.color(1) | curses.A_BOLD)
        if self.data and self.data.get('start') is None:
            self.put(top + 2, 1, (self.data.get('warnings') or ['Weekly reset time unavailable.'])[0], self.color(3))
            self.put(height - 2, 1, 'Esc back · q quit · r refresh', curses.A_DIM)
            return
        tokens = self.data.get('tokens') or {}
        cost = self.data.get('api_cost')
        self.put(top + 1, 1, f"{len(self.turns()):,} prompts · TOTAL {_number(tokens.get('total_tokens'))} tokens · API USD {_money(cost)}",
                 curses.A_BOLD)
        self.put(top + 2, 1, f"Input {_number(tokens.get('input_tokens'))} · Cached {_number(tokens.get('cached_input_tokens'))} · Output {_number(tokens.get('output_tokens'))}")
        for offset, line in enumerate(_cost_breakdown(cost), top + 3):
            self.put(offset, 1, line, self.color(2))
        bottom = height - 3
        table_top = top + 5
        if self.detail:
            self.draw_detail(table_top, bottom, 0, width)
        else:
            chat_width = max(8, min(30, width // 5))
            heading = f"{'PROMPT TIME':11} {'TOKENS':>11} {'API USD':>12}  {'CHAT':{chat_width}}  PROMPT"
            self.put(table_top, 1, heading, curses.A_DIM)
            turns = self.turns()
            first_row = table_top + 1
            capacity = max(0, bottom - first_row)
            self.turn_offset = max(0, min(self.turn_offset, self.turn_index))
            if self.turn_index >= self.turn_offset + capacity:
                self.turn_offset = max(0, self.turn_index - capacity + 1)
            self.turn_area = (first_row, bottom, 0, width)
            for i in range(self.turn_offset, min(len(turns), self.turn_offset + capacity)):
                turn = turns[i]
                at = datetime.fromtimestamp(turn['prompt_at']).strftime('%m-%d %H:%M')
                title = _clip(turn['chat_title'], chat_width)
                title += ' ' * (chat_width - _width(title))
                amount = _number(turn['period_tokens']['total_tokens'])
                money = _money(turn['api_cost_with_children'])
                self.put(first_row + i - self.turn_offset, 1, f"{at} {amount:>11} {money:>12}  {title}  {_clean(turn['prompt'])}",
                         curses.A_REVERSE if i == self.turn_index else 0)
            if not turns:
                self.put(first_row, 1, 'No recorded prompts in this range.' if self.data else 'Loading time range…')
        warnings = [self.error] if self.error else self.data.get('warnings') or []
        self.put(bottom, 1, ' | '.join(warnings) if warnings else
                 'By prompt start · whole turns and linked agents · API estimate',
                 self.color(3) if warnings else curses.A_DIM)
        self.put(height - 2, 1, '↑↓/jk select · Enter detail · Esc back · q quit · r refresh', curses.A_DIM)
        hint = '[ / ] or ← / → previous/next week · w selects period' if self.period_preset == 'weekly' else (
            'Oldest first · cached tokens are included in input')
        self.put(height - 1, 1, hint, curses.A_DIM)

    def draw_chats(self, top: int, bottom: int, width: int) -> None:
        title = f" CHATS ({len(self.threads)}) " + ('+ agents' if self.include_agents else 'root only')
        self.put(top, 0, title, self.color(1) | (curses.A_BOLD if self.focus == 'chats' else 0), width)
        capacity = max(0, bottom - top - 2)
        index = next((i for i, thread in enumerate(self.threads) if thread.get('id') == self.selected), 0)
        self.chat_offset = max(0, min(self.chat_offset, index))
        if index >= self.chat_offset + capacity:
            self.chat_offset = max(0, index - capacity + 1)
        self.chat_area = (top + 2, bottom, 0, width)
        self.put(top + 1, 1, 'TITLE / TOTAL TOKENS', curses.A_DIM, width - 2)
        for row, thread in enumerate(self.threads[self.chat_offset:self.chat_offset + capacity], top + 2):
            chosen = thread.get('id') == self.selected
            marker = '›' if chosen else ' '
            name = ('↳ ' if thread.get('is_agent') else '') + _clean(thread.get('title') or thread.get('id') or 'Untitled')
            total = _number((thread.get('tokens') or {}).get('total_tokens'))
            room = max(1, width - len(total) - 5)
            label = f"{marker} {_clip(name, room)}"
            label += ' ' * max(1, width - 2 - _width(label) - len(total)) + total
            self.put(row, 0, label, curses.A_REVERSE if chosen else self.color(2) if thread.get('active') else 0, width - 1)
        if not self.threads:
            self.put(top + 3, 2, 'No matching chats', curses.A_DIM, width - 3)

    def draw_main(self, top: int, bottom: int, x: int, width: int) -> None:
        selected = self.data.get('selected') or {}
        self.put(top, x + 1, selected.get('title') or 'Select a chat', self.color(1) | curses.A_BOLD, width - 2)
        self.put(top + 1, x + 1, f"{selected.get('model') or 'Unknown model'} · {_clean(selected.get('cwd') or '')}", curses.A_DIM, width - 2)
        tokens = selected.get('tokens') or {}
        self.put(top + 2, x + 1, f"TOTAL {_number(tokens.get('total_tokens'))}   INPUT {_number(tokens.get('input_tokens'))}   OUTPUT {_number(tokens.get('output_tokens'))}", curses.A_BOLD, width - 2)
        self.put(top + 3, x + 1, f"Cached input: {_number(tokens.get('cached_input_tokens'))} (input subset) · Reasoning: {_number(tokens.get('reasoning_output_tokens'))} (output subset)", curses.A_DIM, width - 2)
        tps = selected.get('tps') or self.data.get('tps') or {}
        latest = next(iter(self.turns()), {})
        self.put(top + 4, x + 1, f"TOK/S output {_rate(tps.get('output'))}   latest avg output {_rate(latest.get('avg_tps'))}/s (elapsed turn time)", self.color(2), width - 2)
        agent_turn = (latest.get('children_tokens') or {}).get('total_tokens', 0)
        self.put(top + 5, x + 1, f"LATEST TURN [{latest.get('status', 'N/A')}] {_number((latest.get('tokens') or {}).get('total_tokens'))} tokens (+ agents {_number(agent_turn)}) · {_clean(latest.get('prompt', ''))}", curses.A_BOLD, width - 2)
        children = selected.get('children_total', self.data.get('children_total'))
        if children is not None:
            self.put(top + 6, x + 1, f"Agent descendants ({selected.get('children_count', '?')}): {_number(children)} tokens (separate DB counters)", curses.A_DIM, width - 2)
        self.put(top + 7, x + 1, f"API USD {_money(selected.get('api_cost_with_children'))} (root {_money(selected.get('api_cost'))} + agents {_money(selected.get('children_api_cost'))}) · Standard token estimate", self.color(2), width - 2)
        for offset, line in enumerate(_cost_breakdown(selected.get('api_cost_with_children')), 8):
            self.put(top + offset, x + 1, line, self.color(2), width - 2)
        row = top + 11
        if getattr(self, 'limits_view', False):
            self.draw_limits(row, bottom, x, width)
        elif self.detail:
            self.draw_detail(row, bottom, x, width)
        else:
            self.draw_history(row, bottom, x, width)

    def draw_history(self, top: int, bottom: int, x: int, width: int) -> None:
        turns = self.turns()
        title = 'TURN HISTORY'
        self.put(top, x + 1, f"{title} ({len(turns)}) · newest first · Enter opens text", self.color(1) | (curses.A_BOLD if self.focus == 'history' else 0), width - 2)
        top += 1
        heading = 'START        STATUS       TOTAL   OUTPUT OUTAVG/S MODEL               API USD  PROMPT'
        self.put(top, x + 1, heading, curses.A_DIM, width - 2)
        top += 1
        capacity = max(0, bottom - top)
        self.turn_offset = max(0, min(self.turn_offset, self.turn_index))
        if self.turn_index >= self.turn_offset + capacity:
            self.turn_offset = max(0, self.turn_index - capacity + 1)
        self.turn_area = (top, bottom, x, x + width)
        for index in range(self.turn_offset, min(len(turns), self.turn_offset + capacity)):
            turn = turns[index]
            tokens = turn.get('tokens') or {}
            label = f"{_date(turn.get('started_at')):12} {_clean(turn.get('status') or 'unknown'):9.9} {_number(tokens.get('total_tokens')):>11} {_number(tokens.get('output_tokens')):>8} {_rate(turn.get('avg_tps')):>8} {_clip(turn.get('model') or 'unknown', 15):15} {_money(turn.get('api_cost_with_children')):>12}  {_clean(turn.get('prompt'))}"
            self.put(top + index - self.turn_offset, x + 1, label, curses.A_REVERSE if index == self.turn_index and self.focus == 'history' else 0, width - 2)
        if not turns:
            self.put(top, x + 1, 'No turns recorded for this chat', curses.A_DIM, width - 2)

    def draw_detail(self, top: int, bottom: int, x: int, width: int) -> None:
        turns = self.turns()
        if not turns:
            return
        turn = turns[self.turn_index]
        tokens = turn.get('tokens') or {}
        self.put(top, x + 1, f"TURN DETAIL · {self.turn_index + 1}/{len(turns)} · Esc closes · ↑↓ scroll", self.color(1) | curses.A_BOLD, width - 2)
        lines = [f"{turn.get('status') or 'unknown'} · {_date(turn.get('started_at'))} → {_date(turn.get('ended_at'))} · average OUTPUT {_rate(turn.get('avg_tps'))} tok/s", f"Model: {turn.get('model') or 'unknown'}", f"Total {_number(tokens.get('total_tokens'))} · Input {_number(tokens.get('input_tokens'))} · Output {_number(tokens.get('output_tokens'))}", f"Cached input {_number(tokens.get('cached_input_tokens'))} · Reasoning output {_number(tokens.get('reasoning_output_tokens'))}", f"Usage source: {turn.get('usage_source', 'reported telemetry')}", f"Linked child-agent response tokens (separate): {_number((turn.get('children_tokens') or {}).get('total_tokens', 0))}"]
        cost = turn.get('api_cost') or {}
        if self.mode == 'time':
            lines.insert(0, f"Chat: {turn['chat_title']} · {self.local_minute(turn['prompt_at'])}")
        lines += [f"API USD: root {_money(cost)} + linked agents {_money(turn.get('children_api_cost'))} = {_money(turn.get('api_cost_with_children'))}", 'Agent models: ' + (', '.join((turn.get('children_api_cost') or {}).get('models') or []) or 'none recorded'), 'API estimate = current Standard token prices; not subscription billing or internal OpenAI costs.', f"Root cache writes: {_number(tokens.get('cache_write_input_tokens', 0))} (input subset); reasoning already included in output."]
        lines += ['Combined cost breakdown (root + linked agents):']
        lines += _cost_breakdown(turn.get('api_cost_with_children'))
        lines += [f"Root priced calls: {cost.get('calls', 0)} · >272K input calls: {cost.get('long_context_calls', 0)} · unpriced: {cost.get('unpriced_calls', 0)}", f"Rates checked {cost.get('rate_date', 'N/A')} · {cost.get('source', '')}"]
        notes = list((turn.get('api_cost_with_children') or {}).get('notes') or [])
        notes.append(cost.get('exclusions') or 'Separate tool fees and service-tier/regional adjustments excluded.')
        for note in notes:
            lines += _wrap(note, max(2, width - 3))
        lines += ['', 'PROMPT'] + _wrap(turn.get('prompt') or '', max(2, width - 3))
        if turn.get('answer') is not None:
            lines += ['', 'ANSWER'] + _wrap(turn.get('answer'), max(2, width - 3))
        capacity = max(0, bottom - top - 1)
        self.detail_offset = min(self.detail_offset, max(0, len(lines) - capacity))
        for offset, line in enumerate(lines[self.detail_offset:self.detail_offset + capacity], 1):
            self.put(top + offset, x + 1, line, 0, width - 2)

    def draw_limits(self, top: int, bottom: int, x: int, width: int) -> None:
        self.put(top, x + 1, 'ALL ACCOUNT LIMITS · l/Esc closes · ↑↓ scroll', self.color(1), width - 2)
        lines = []
        for limit in self.data.get('limits') or []:
            for key in ('primary', 'secondary'):
                window = limit.get(key)
                if window:
                    used = window.get('used_percent')
                    try:
                        percent = f"{float(used):.{self.decimals}f}%"
                    except (TypeError, ValueError):
                        percent = 'N/A'
                    lines += [f"{limit.get('id')} / {key}: {percent} used; reset {_date(window.get('resets_at'))}", f"{limit.get('source')} · {_age(limit.get('observed_at'))}"]
        capacity = max(0, bottom - top - 1)
        self.detail_offset = min(self.detail_offset, max(0, len(lines) - capacity))
        for n, line in enumerate(lines[self.detail_offset:self.detail_offset + capacity], 1):
            self.put(top + n, x + 1, line, 0, width - 2)

    def select_chat(self, index: int) -> None:
        if not self.threads:
            return
        index = max(0, min(index, len(self.threads) - 1))
        self.follow = False
        chosen = self.threads[index].get('id')
        if chosen != self.selected:
            self.selected = chosen
            self.turn_index = self.turn_offset = self.detail_offset = 0
            self.turn_id = None
            self.detail = False
            cached = self._view_cache.get(chosen)
            if cached is not None:
                self.data = cached
            else:
                row = self.threads[index]
                self.data = {'selected':dict(row, turns=[], warnings=[], coverage='Loading chat history...'), 'threads':self.threads}
            self.next_refresh = 0

    def move(self, direction: int) -> None:
        if self.detail or getattr(self, 'limits_view', False):
            self.detail_offset = max(0, self.detail_offset + direction)
        elif self.focus == 'chats':
            index = next((i for i, thread in enumerate(self.threads) if thread.get('id') == self.selected), 0)
            self.select_chat(index + direction)
        else:
            turns = self.turns()
            self.turn_index = max(0, min(self.turn_index + direction, len(turns) - 1))
            if turns:
                self.turn_id = self.turn_key(turns[self.turn_index])

    def mouse(self) -> None:
        try:
            _, mouse_x, mouse_y, _, state = curses.getmouse()
        except curses.error:
            return
        if state & getattr(curses, 'BUTTON4_PRESSED', 0):
            self.move(-1)
            return
        if state & getattr(curses, 'BUTTON5_PRESSED', 0):
            self.move(1)
            return
        if not state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
            return
        for y, x, key, tab, _ in self.view_tabs(self.screen.getmaxyx()[1]):
            if mouse_y == y and x <= mouse_x < x + len(tab):
                self.key(key)
                return
        if self.mode == 'time' and self.detail:
            return
        for panel, area in (('chats', self.chat_area), ('history', self.turn_area)):
            if self.mode == 'time' and panel == 'chats':
                continue
            top, bottom, left, right = area
            if top <= mouse_y < bottom and left <= mouse_x < right:
                self.focus = panel
                if panel == 'chats':
                    self.select_chat(self.chat_offset + mouse_y - top)
                elif self.turns():
                    self.turn_index = min(len(self.turns()) - 1, self.turn_offset + mouse_y - top)
                    self.turn_id = self.turn_key(self.turns()[self.turn_index])
                    if state & curses.BUTTON1_DOUBLE_CLICKED:
                        self.detail = True
                        self.detail_offset = 0
                break

    def key(self, key: Any) -> bool:
        if self.range_fields is not None:
            self.range_key(key)
            return True
        if self.weekly_picker:
            if key in ('q', 'Q'):
                return False
            self.weekly_picker_key(key)
            return True
        if self.searching:
            if key in ('\n', '\r', curses.KEY_ENTER):
                self.searching = False
                self.next_refresh = 0
            elif key == '\x1b':
                self.query = self.search_before
                self.searching = False
                self.next_refresh = 0
            elif key in (curses.KEY_BACKSPACE, '\x7f', '\x08'):
                self.query = self.query[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.query += key
            return True
        if key in ('q', 'Q'):
            return False
        if self.period_navigation(key):
            return True
        if key == '\t':
            self.focus = 'history' if self.focus == 'chats' else 'chats'
            self.detail = False
            self.limits_view = False
        elif key in (curses.KEY_UP, 'k'):
            self.move(-1)
        elif key in (curses.KEY_DOWN, 'j'):
            self.move(1)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.move(-8 if key == curses.KEY_PPAGE else 8)
        elif key in ('\n', '\r', curses.KEY_ENTER):
            if self.focus == 'chats':
                self.focus = 'history'
            elif self.turns():
                self.detail = not self.detail
                self.detail_offset = 0
        elif key == '\x1b':
            if not self.detail and (not getattr(self, 'limits_view', False)):
                self.focus = 'chats'
            self.detail = False
            self.limits_view = False
        elif key == 'l':
            self.limits_view = not getattr(self, 'limits_view', False)
            self.focus = 'history'
            self.detail_offset = 0
        elif key == '/':
            self.searching = True
            self.search_before = self.query
        elif key == 'a':
            self.include_agents = not self.include_agents
            self.next_refresh = 0
        elif key == 'f':
            self.follow = not self.follow
            self.next_refresh = 0
        elif key == 'd':
            self.decimals = 3 if self.decimals == 2 else 2
        elif key == 'r':
            self.next_refresh = 0
        elif key == curses.KEY_MOUSE:
            self.mouse()
        return True

    def run(self) -> None:
        self.setup()
        while True:
            self.poll_results()
            if time.monotonic() >= self.next_refresh and not self.searching and self.range_fields is None:
                self.fetch()
            self.draw(loading=self._loading)
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if not self.key(key):
                return

def run_ui(monitor: Any, initial_thread: str | None=None, refresh: float=5.0, decimals: int=2) -> None:
    """Run the interactive dashboard, restoring terminal state on exit/errors."""
    try:
        curses.wrapper(lambda screen: _Dashboard(screen, monitor, initial_thread, refresh, decimals).run())
    except KeyboardInterrupt:
        return
