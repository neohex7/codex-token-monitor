"""Incremental reconstruction of recorded Codex weekly reset cycles."""
import json
import math
from pathlib import Path

from .telemetry import normalize_limit, timestamp

WEEK = 7 * 86400
RESET_TOLERANCE = 300


def observation(limit):
    if limit.get('id') != 'codex':
        return None
    at = timestamp(limit.get('observed_at'))
    for slot in ('primary', 'secondary'):
        window = limit.get(slot)
        if not window or window.get('window_minutes') != 10080:
            continue
        end = timestamp(window.get('resets_at'))
        if at is not None and end is not None and math.isfinite(at) and math.isfinite(end):
            if end - WEEK <= at < end:
                return end, at
    return None


class WeeklyHistory:
    def __init__(self):
        self.files = {}

    def read(self, path):
        path = Path(path)
        key = str(path)
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            state = self.files.get(key)
            if state is None or state['identity'] != identity or stat.st_size < state['offset']:
                state = dict(identity=identity, offset=0, cycles={})
                self.files[key] = state
            if state.get('stat') == (stat.st_size, stat.st_mtime_ns):
                return True
            with path.open('rb') as stream:
                stream.seek(state['offset'])
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        break
                    state['offset'] = stream.tell()
                    if b'"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get('payload') or {}
                        if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                            continue
                        raw = payload.get('rate_limits')
                        if not isinstance(raw, dict):
                            continue
                        item = observation(normalize_limit(raw, timestamp(event.get('timestamp')), 'rollout'))
                        if item:
                            end, at = item
                            previous = state['cycles'].get(end)
                            state['cycles'][end] = min(at, previous) if previous is not None else at
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        continue
            state['stat'] = (stat.st_size, stat.st_mtime_ns)
            return True
        except OSError:
            return False

    def periods(self, paths, limits, now):
        available = set(map(str, paths))
        missing = sum(not self.read(path) for path in available)
        samples = []
        for path in available:
            samples.extend(self.files.get(path, {}).get('cycles', {}).items())
        live = []
        for limit in limits:
            item = observation(limit)
            if item:
                samples.append(item)
                if limit.get('source') == 'account/rateLimits/read' and item[0] - WEEK <= now < item[0]:
                    live.append(item)
        groups = []
        for end, at in sorted(samples):
            # Backend reset countdowns drift by dozens of seconds in real logs.
            if groups and abs(end - groups[-1]['reset_at']) <= RESET_TOLERANCE:
                group = groups[-1]
                group['observed_at'] = min(group['observed_at'], at)
            else:
                groups.append(dict(reset_at=end, observed_at=at))
        current = max(live, key=lambda item: item[1])[0] if live else None
        if current is not None:
            for group in groups:
                if abs(group['reset_at'] - current) <= RESET_TOLERANCE:
                    group['reset_at'] = current
        result = []
        for i, group in enumerate(groups):
            reset = group['reset_at']
            start = reset - WEEK
            if start > now:
                continue
            boundary = groups[i + 1]['reset_at'] - WEEK if i + 1 < len(groups) else reset
            end = min(reset, boundary)
            if end <= start:
                continue
            active = start <= now < end and (current is None or abs(reset - current) <= RESET_TOLERANCE)
            result.append(dict(reset_at=reset, start=start, end=min(end, now),
                               scheduled_end=reset, current=active, early_reset=end < reset))
        result.reverse()
        warnings = []
        if missing:
            warnings.append(f'{missing} rollout files unavailable; older weekly cycles may be missing.')
        return result, warnings
