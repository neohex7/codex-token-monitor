#!/usr/bin/env bash
set -euo pipefail
python_bin="${CODEX_MONITOR_PYTHON:-python3}"
project_dir="$("$python_bin" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "$0")"
if command -v gnome-terminal >/dev/null 2>&1; then
    exec gnome-terminal --title='Codex Token Monitor' --geometry=156x44 --working-directory="$project_dir" -- "$project_dir/start.sh" "$@"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
    exec x-terminal-emulator -e "$project_dir/start.sh" "$@"
else
    echo 'No supported terminal emulator found. Run start.sh in an existing terminal.' >&2
    exit 127
fi
