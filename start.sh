#!/usr/bin/env bash
set -euo pipefail
python_bin="${CODEX_MONITOR_PYTHON:-python3}"
project_dir="$("$python_bin" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "$0")"
cd -- "$project_dir"
exec "$python_bin" -m codex_token_monitor "$@"
