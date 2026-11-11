"""Date presets and input controls for the terminal's time view."""
from datetime import datetime
import curses
import time


PERIOD_LABELS = {'today': 'Today', '24h': 'Last 24 hours', 'custom': 'Custom',
                 '7d': 'Last 7 days', '30d': 'Last 30 days', 'weekly': 'Since weekly reset'}
PERIOD_KEYS = {'1': 'today', '2': '24h', '3': '7d', '4': '30d', '5': 'weekly'}
ROLLING_SECONDS = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400}


def local_minute(value):
    return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M')


def parse_minute(text):
    text = text.strip()
    at = datetime.strptime(text, '%Y-%m-%d %H:%M').timestamp()
    if local_minute(at) != text:
        raise ValueError('Use a valid local date and time: YYYY-MM-DD HH:MM.')
    return at


class PeriodView:
    def init_period(self):
        self.mode = 'chat'
        self.period_preset = 'today'
        self.custom_period = None
        self.range_fields = None
        self.range_field = 0
        self.range_error = ''
        self.weekly_reset = None
        self.weekly_picker = False
        self.weekly_periods = []
        self.weekly_index = 0
        self.weekly_offset = 0
        self.weekly_picker_warnings = []

    local_minute = staticmethod(local_minute)

    @staticmethod
    def period_bounds(request, now):
        _, preset, custom = request
        if preset == 'custom':
            start, end = custom
            return start, now if end is None else end
        if preset == 'weekly':
            return None, now
        if preset in ROLLING_SECONDS:
            return now - ROLLING_SECONDS[preset], now
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp(), now

    def view_tabs(self, width):
        editing = self.range_fields is not None
        is_time = self.mode == 'time' or editing
        preset = 'custom' if editing else self.period_preset
        tabs = [('c', 'Chat', not is_time), ('t', 'Time', is_time),
                ('1', 'Today', is_time and preset == 'today'),
                ('2', 'Last 24h', is_time and preset == '24h'),
                ('3', 'Last 7d', is_time and preset == '7d'),
                ('4', 'Last 30d', is_time and preset == '30d'),
                ('5', 'Weekly Reset', is_time and preset == 'weekly'),
                ('w', 'Periods', self.weekly_picker),
                ('6', 'Custom', is_time and preset == 'custom')]
        y, x = 2, 1
        for key, label, selected in tabs:
            tab = f"[{key} {label}]"
            if x > 1 and x + len(tab) > width:
                y, x = y + 1, 1
            yield y, x, key, tab, selected
            x += len(tab) + 1

    def switch_view(self, mode, preset=None, force=False):
        if not force and mode == self.mode and (preset is None or preset == self.period_preset):
            return
        self.mode = mode
        if preset:
            self.period_preset = preset
        self.focus = 'history' if mode == 'time' else 'chats'
        self.turn_index = self.turn_offset = self.detail_offset = 0
        self.turn_id = None
        self.detail = self.limits_view = False
        self.turn_area = (0, 0, 0, 0)
        self.data = self._view_cache.get(self.selected, {}) if mode == 'chat' else {}
        self.error = ''
        self.next_refresh = 0

    def draw_range_editor(self):
        self.put(4, 1, 'CUSTOM TIME RANGE · local time · YYYY-MM-DD HH:MM', curses.A_BOLD)
        for i, label in enumerate(('From (inclusive)', 'To (exclusive)')):
            value = self.range_fields[i] or ('YYYY-MM-DD HH:MM' if i == 0 else 'Now (leave blank)')
            self.put(6 + i, 1, f"{label:17} {value}{'▏' if i == self.range_field else ''}",
                     curses.A_REVERSE if i == self.range_field else 0)
        self.put(9, 1, 'Click or Tab to switch · Enter: next/apply · Esc: cancel')
        self.put(10, 1, 'Ctrl+U clears the field · Backspace edits', curses.A_DIM)
        self.put(11, 1, self.range_error, self.color(4))

    def range_key(self, key):
        if key == '\x1b':
            self.range_fields = None
        elif key in ('\t', curses.KEY_BTAB):
            self.range_field = 1 - self.range_field
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            self.range_field = int(key == curses.KEY_DOWN)
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return
            if y in (6, 7) and 1 <= x < 42 and state & (
                    curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
                self.range_field = y - 6
        elif key in ('\n', '\r', curses.KEY_ENTER):
            try:
                start = parse_minute(self.range_fields[0])
            except (ValueError, OverflowError, OSError):
                self.range_field = 0
                self.range_error = 'From: enter a valid local time (YYYY-MM-DD HH:MM).'
                return
            if self.range_field == 0:
                self.range_field = 1
                self.range_error = ''
                return
            try:
                end = parse_minute(self.range_fields[1]) if self.range_fields[1].strip() else None
            except (ValueError, OverflowError, OSError):
                self.range_error = 'To: enter a valid local time or leave blank for now.'
                return
            if start >= (time.time() if end is None else end):
                self.range_error = 'The end must be later than the start.'
                return
            self.custom_period = (start, end)
            self.range_fields = None
            self.switch_view('time', 'custom', force=True)
        elif key in (curses.KEY_BACKSPACE, '\x7f', '\x08'):
            self.range_fields[self.range_field] = self.range_fields[self.range_field][:-1]
            self.range_error = ''
        elif key == '\x15':
            self.range_fields[self.range_field] = ''
            self.range_error = ''
        elif isinstance(key, str) and len(key) == 1 and key in '0123456789-: ':
            if len(self.range_fields[self.range_field]) < 16:
                self.range_fields[self.range_field] += key
                self.range_error = ''

    def period_navigation(self, key):
        if key == 'w':
            if self.data.get('weekly_periods'):
                self.weekly_periods = self.data['weekly_periods']
                chosen = self.data.get('weekly_period') or {}
                self.weekly_index = next((i for i, p in enumerate(self.weekly_periods)
                                          if p['reset_at'] == chosen.get('reset_at')), 0)
            self.weekly_picker = True
            self.next_refresh = 0
            self.error = ''
            return True
        if key == '5':
            self.weekly_reset = None
            self.switch_view('time', 'weekly', force=True)
            return True
        if key == '6':
            self.range_fields = ([local_minute(value) if value is not None else '' for value in self.custom_period]
                                 if self.custom_period else ['', ''])
            self.range_field = 0
            self.range_error = ''
            return True
        if key in ('c', 't') or key in PERIOD_KEYS:
            self.switch_view('chat' if key == 'c' else 'time', PERIOD_KEYS.get(key))
            return True
        if self.mode != 'time':
            return False
        if self.period_preset == 'weekly' and not self.detail and key in ('[', ']', curses.KEY_LEFT, curses.KEY_RIGHT):
            periods = self.data.get('weekly_periods') or []
            if periods:
                chosen = self.data.get('weekly_period') or {}
                index = next((i for i, p in enumerate(periods)
                              if p['reset_at'] == chosen.get('reset_at')), 0)
                index = max(0, min(len(periods) - 1,
                                  index + (1 if key in ('[', curses.KEY_LEFT) else -1)))
                self.choose_weekly(periods[index])
            return True
        if key == '\x1b':
            if self.detail:
                self.detail = False
            else:
                self.switch_view('chat')
        elif key in ('\n', '\r', curses.KEY_ENTER) and self.turns():
            self.detail = not self.detail
            self.detail_offset = 0
        elif key in (curses.KEY_UP, 'k', curses.KEY_DOWN, 'j', curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.move(-8 if key == curses.KEY_PPAGE else 8 if key == curses.KEY_NPAGE
                      else -1 if key in (curses.KEY_UP, 'k') else 1)
        elif key == 'r':
            self.next_refresh = 0
        elif key == curses.KEY_MOUSE:
            self.mouse()
        return True

    def choose_weekly(self, period):
        self.weekly_picker = False
        self.weekly_reset = None if period['current'] else period['reset_at']
        self.switch_view('time', 'weekly', force=True)

    def weekly_picker_key(self, key):
        if key == '\x1b':
            self.weekly_picker = False
            self.next_refresh = 0
        elif key in ('\n', '\r', curses.KEY_ENTER):
            if self.weekly_periods:
                self.choose_weekly(self.weekly_periods[self.weekly_index])
        elif key in (curses.KEY_UP, 'k', curses.KEY_DOWN, 'j', curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = -8 if key == curses.KEY_PPAGE else 8 if key == curses.KEY_NPAGE else (
                -1 if key in (curses.KEY_UP, 'k') else 1)
            self.weekly_index = max(0, min(max(0, len(self.weekly_periods) - 1), self.weekly_index + step))
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return
            top, bottom = self.weekly_picker_area
            if top <= y < bottom and x >= 1 and state & (
                    curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
                index = self.weekly_offset + y - top
                if index < len(self.weekly_periods):
                    self.weekly_index = index
                    self.choose_weekly(self.weekly_periods[index])
        elif key == 'r':
            self.next_refresh = 0

    def draw_weekly_picker(self, top, bottom):
        self.put(top, 1, 'WEEKLY PERIODS · newest first · Enter or click selects · Esc cancels', curses.A_BOLD)
        top += 2
        capacity = max(0, bottom - top)
        self.weekly_offset = min(self.weekly_offset, self.weekly_index)
        if self.weekly_index >= self.weekly_offset + capacity:
            self.weekly_offset = max(0, self.weekly_index - capacity + 1)
        self.weekly_picker_area = (top, bottom)
        for i in range(self.weekly_offset, min(len(self.weekly_periods), self.weekly_offset + capacity)):
            period = self.weekly_periods[i]
            status = 'Current' if period['current'] else 'Early reset' if period['early_reset'] else 'Previous'
            label = f"{i + 1:>3}. {self.local_minute(period['start'])} → {self.local_minute(period['end'])} · {status}"
            self.put(top + i - self.weekly_offset, 1, label,
                     curses.A_REVERSE if i == self.weekly_index else 0)
        if not self.weekly_periods:
            self.put(top, 1, 'Loading recorded weekly periods…' if self._loading else 'No weekly reset history available.')
        self.put(bottom, 1, self.error or ' | '.join(self.weekly_picker_warnings) or
                 'Only recorded Codex cycles are listed; gaps in local history are not filled in.',
                 self.color(3))
        self.put(bottom + 1, 1, '↑↓/jk select · Enter open · Esc back · r refresh · q quit', curses.A_DIM)
