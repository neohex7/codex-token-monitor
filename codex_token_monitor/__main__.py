from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
from .telemetry import Monitor
from .ui import _money


def main():
    parser = argparse.ArgumentParser(description='Read-only terminal dashboard for local Codex token usage.')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')))
    parser.add_argument('--codex-bin', help='Codex executable or command for optional live quota reads (also CODEX_MONITOR_CODEX_BIN)')
    parser.add_argument('--thread', help='Chat title, ID, or unique part of either')
    parser.add_argument('--refresh', type=float, default=5.0, help='Refresh seconds (minimum 1; default 5)')
    parser.add_argument('--decimals', type=int, choices=(2,3), default=2, help='Quota display decimals, not source precision')
    parser.add_argument('--offline', action='store_true', help='Read local files only; no live quota RPC')
    parser.add_argument('--once', action='store_true', help='Print one readable snapshot and exit')
    parser.add_argument('--json', action='store_true', help='Print one machine-readable snapshot and exit')
    parser.add_argument('--list', action='store_true', help='List local chat titles and database token totals')
    args = parser.parse_args()
    if args.refresh < 1 or not __import__('math').isfinite(args.refresh):
        parser.error('--refresh must be a finite number >= 1')
    monitor = Monitor(args.codex_home)
    if args.list:
        for row in monitor.read_threads():
            if not row['is_agent']:
                title = ' '.join(row['title'].split())
                title = ''.join(c for c in title if c.isprintable())
                total = row['tokens']['total_tokens']
                count = f'{total:,}' if total is not None else 'not loaded'
                print(f"{row['id']}  {count:>15}  {title[:120]}")
        monitor.close()
        return 0
    selected = None
    if args.thread:
        rows = monitor.read_threads()
        candidates = [r for r in rows if args.thread.casefold() in (r['title']+' '+r['id']).casefold() and not r['is_agent']]
        exact = [r for r in candidates if args.thread.casefold() in (r['title'].casefold(),r['id'].casefold())]
        candidates = exact or candidates
        if len(candidates) != 1:
            monitor.close()
            parser.error('Chat not found or ambiguous; use --list and pass its ID with --thread')
        selected = candidates[0]['id']
    if not args.offline:
        from .limits import LimitsClient
        monitor.limits_client = LimitsClient(monitor.home, interval=max(5,args.refresh), binary=args.codex_bin)
    try:
        if args.once or args.json:
            if monitor.limits_client:
                monitor.limits_client.ready.wait(timeout=4)
            snapshot = monitor.snapshot(selected)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            else:
                selected = snapshot['selected']
                if not selected:
                    print('No local Codex chats found.')
                    return 0
                safe = lambda s: ''.join(c for c in str(s) if c.isprintable())
                print(f"Codex Token Monitor | {safe(selected['title'])}")
                print(f"Chat tokens: {selected['tokens']['total_tokens']:,}")
                print(f"Input: {selected['tokens']['input_tokens']:,} | cached subset: {selected['tokens']['cached_input_tokens']:,} | output: {selected['tokens']['output_tokens']:,}")
                print(f"Child agents (separate DB counters): {selected['children_total']:,}")
                print(f"API token estimate USD: root {_money(selected['api_cost'])} | agents {_money(selected['children_api_cost'])} | combined {_money(selected['api_cost_with_children'])}")
                print(f"Standard API token rates checked {selected['api_cost']['rate_date']}; includes recorded compaction. Separate tool fees, service-tier and regional adjustments excluded.")
                for turn in reversed(selected['turns'][-10:]):
                    average = f"{turn['avg_tps']:.2f}/s" if turn['avg_tps'] is not None else 'N/A'
                    print(f"{turn['status']:11} {turn['tokens']['total_tokens']:>13,} | {safe(turn['model'])} | avg output {average}")
                    print(f"  API {_money(turn['api_cost_with_children'])} (root {_money(turn['api_cost'])}, linked agents {_money(turn['children_api_cost'])}) | {safe(turn['prompt'])[:100]}")
                for warning in snapshot['warnings']+selected['warnings']:
                    print('Note: '+warning)
                print('Token rates require two live samples. Counts arrive in reported batches, not each generated token.')
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser.error('Interactive mode requires a terminal; use --once or --json for snapshots')
            from .ui import run_ui
            run_ui(monitor, initial_thread=selected, refresh=args.refresh, decimals=args.decimals)
    except KeyboardInterrupt:
        return 130
    finally:
        monitor.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
