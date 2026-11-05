# Codex Token Monitor

A read-only terminal dashboard for local Codex chats, token usage, turn history, output speed, and optional account usage limits.

Requires Python 3.10 or newer and no third-party Python packages. Interactive mode needs a terminal and Python's `curses` module. Live account limits also need a locally installed Codex executable and an existing Codex sign-in. No API key or model request is needed.

## Run

From the project directory, in an existing terminal:

```bash
python3 -m codex_token_monitor
python3 -m codex_token_monitor --thread 'Example chat'
```

The module entry point can run on systems with Python and a working `curses` terminal. The included Bash launchers target Linux:

```bash
./start.sh
./open-terminal.sh
```

`open-terminal.sh` opens a separate terminal using `gnome-terminal`, or falls back to `x-terminal-emulator`. It requests a 156 × 44 window where supported. On other platforms, use the module entry point in an existing terminal.

The default refresh interval is **5 seconds**. The first load of a large chat can take several seconds; subsequent updates read new rollout lines incrementally. Data and parsed views are cached only in memory. Press `q` to exit.

Navigation responds without waiting for telemetry refresh: data is loaded in a background thread, and Enter, Tab, Esc, and quitting remain available while a chat loads. Previously opened chats have cached views. Standalone Esc uses a 20 ms detection delay.

### Optional Linux command

Run this from the project directory to make the terminal launcher available as `codex-token-monitor`:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$(pwd)/open-terminal.sh" "$HOME/.local/bin/codex-token-monitor"
```

Add `$HOME/.local/bin` to your `PATH` if needed. The symbolic link points to this checkout, so keep it at the same location. If the destination already exists, inspect it before replacing it.

### Executable configuration

Live account limits use the Codex executable selected in this order: `--codex-bin`, `CODEX_MONITOR_CODEX_BIN`, `codex` on `PATH`, then standard Linux/macOS desktop app bundle locations. An invalid explicit override is reported rather than silently falling back. Offline mode does not need a Codex executable.

```bash
python3 -m codex_token_monitor --codex-bin /path/to/codex
```

The Linux Bash launchers use `python3` by default. Set `CODEX_MONITOR_PYTHON` to select another Python executable:

```bash
CODEX_MONITOR_PYTHON=/path/to/python3 ./start.sh
```

## Dashboard

- Local chat titles and IDs, including archived chats when their local records exist.
- Chat input, output, cached input, and reasoning counters. **Cached input is included in input and total usage; reasoning is included in output.** These subsets are never added again or subtracted from total consumption.
- The latest user turn, its status, and usage while work is in progress. Messages steering ongoing work remain in that turn.
- Available turn history with times, status, model, token totals, average output tokens per second, estimated API token cost, and abbreviated prompts. Enter opens the full prompt, counter source, model details, and cost breakdown.
- Current output speed, calculated as the change in reported output divided by elapsed time between samples. The first sample is `N/A`; no new tokens means zero. Average turn speed is output divided by the entire turn duration, including tools and waiting. Reported reasoning counts as output. Usage arrives in batches, so these figures are not pure model decoding speed. JSON also retains the current input-plus-output speed field.
- An optional account limits panel showing usage windows, resets, source, and observation age. Task-level limit percentages are not inferred from token totals.
- Separate available child-agent counters. They are not automatically added to the parent chat token total, because inherited fork history and different counter sources can overlap. Linked child-agent API estimates are shown separately and in combined cost figures.

## Controls

| Key | Action |
| --- | --- |
| Up / Down or `j` / `k` | Select a chat or turn |
| Tab | Switch between chats and turn history |
| Enter | Focus history or open turn details |
| `/` | Search titles or IDs; Enter applies the search |
| Esc | Close details or limits, cancel search, or return from history to chats |
| `f` | Toggle following the most recently active local chat |
| `a` | Show or hide child-agent chats |
| `d` | Toggle limit display between two and three decimal places |
| `l` | Open account limits; Up / Down scrolls |
| `r` | Refresh immediately |
| `q` | Exit |

A manually selected chat stays selected until follow mode is enabled with `f`. Follow mode uses recorded local activity, not the focused desktop app window. Mouse selection works in terminals with `curses` mouse support. A 156 × 44 terminal is recommended; narrower terminals switch panels with Tab.

## Data and accounting limits

### Token counters

Totals represent reported model inputs and outputs, including repeated context and tool results. They are not a word count of the visible question and answer. Only chats with local Codex records are covered; remote-only sessions and ordinary ChatGPT conversations are not included.

Older `event_msg/token_count` records and newer `token_usage_record` records can use different starting points. The monitor does not add both histories as independent consumption. It uses the latest cumulative chat counter when available and displays newer response records that have not yet appeared in that counter. Turn totals use explicit `turn_token_usage` or unique response records; older turns use differences between cumulative counters. A warning appears when turn totals and the historical chat counter disagree. JSON includes the raw modern response total and the independent database counter.

Forks can contain inherited history. The monitor warns about this and does not claim that the entire account is globally deduplicated. Context compaction is not a new user turn.

Deleted, unreadable, or incomplete rollouts cannot be reconstructed. Missing data and differences from database counters are reported. An incomplete final line waits for a later refresh; malformed lines are skipped with a warning.

### API token estimates

`API USD` is an **estimated Standard API token equivalent**, not a subscription charge, account bill, internal OpenAI resource cost, or percentage of a plan limit.

The bundled rate table in `codex_token_monitor/pricing.py` is dated **2026-11-05**. Rates are not downloaded or updated at startup, and this documentation does not independently verify their current accuracy. Historical turns are priced using that bundled table, rather than rates at the time the turn ran. Review the table against the official sources before relying on an estimate.

The table covers GPT-6.1 Sol/Terra, GPT-6 Astra/Sol/Luna, GPT-5.6 Sol/Terra/Luna, and GPT-5.5, including dated identifiers for those models. Its GPT-5.6 Sol entry is marked as promotional through November 30. Unsupported models or incomplete records produce `N/A` or a known partial subtotal such as `>=$...`.

Estimates are calculated per recorded model call before aggregation. Ordinary input, cache reads, cache writes, and output are priced separately. Reasoning is already part of output and is not charged again. Cache writes are removed from ordinary input and charged once: 1.25 times the input rate for 30 minutes or 2.5 times for 12 hours. Unspecified write lifetimes use the 30-minute default with a note; unverified GPT-5.5 cache-write pricing is unavailable. For each individual call above 272,000 input tokens, the bundled calculation applies twice the input/cache rate and 1.5 times the output rate. Several small calls do not become one long-context call.

The turn cost column includes the parent turn and linked child agents. Details separate parent and child costs, models, and token categories. Chat estimates include available response records and recorded context compaction, so they can cover more tokens than the historical chat counter. Linked child records are not added twice. Missing calls or unlinked child usage make an estimate incomplete.

Separate web, image, file, container, MCP, and other tool charges are not included, nor are Fast/Ultrafast/Flex/Batch pricing, regional adjustments, taxes, or negotiated discounts. Tool-result tokens reported as model input are already included. Calculations use decimal arithmetic without rounding each call; the terminal displays six decimal places and JSON retains the full calculated amount. Compaction does not guarantee savings.

Bundled source references: [API pricing](https://developers.openai.com/api/docs/pricing), [models](https://developers.openai.com/api/docs/models), and [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

### Account percentages

The limits panel displays the precision returned by its source. Formatting a whole-number value such as `29` as `29.00%` or `29.000%` does not establish that precision. Source decimals are preserved when available. No token-to-plan-percentage conversion is inferred.

## Sources and privacy

The monitor opens `$CODEX_HOME/state_*.sqlite` in read-only mode, then reads existing `.jsonl` rollouts referenced by the database or found in `sessions` and `archived_sessions`. The default Codex home is `~/.codex`; override it with `CODEX_HOME` or `--codex-home`.

For fresh limits, it starts its own Codex app-server process and sends only `initialize`, `initialized`, and the read-only `account/rateLimits/read` request. Polling occurs at least five seconds apart and uses the existing Codex sign-in. The monitor does not submit prompts, start model turns, purchase or reset usage credits, write conversations, or control the running desktop app. It stops its own app-server process on exit.

If live limits are unavailable, the monitor can show the latest timestamped limits in local records. For local file reads without live limits requests:

```bash
python3 -m codex_token_monitor --offline
```

Protocol reference: [OpenAI app-server documentation](https://learn.chatgpt.com/docs/app-server), including `account/rateLimits/read` and `thread/tokenUsage/updated`.

Snapshot output can contain private data. `--list` includes chat titles and IDs; `--once` includes selected chat information and prompt excerpts; `--json` includes titles and prompt text. Review output before sharing it. The monitor has no automatic export or prompt-upload feature.

## Command examples

```bash
python3 -m codex_token_monitor --list
python3 -m codex_token_monitor --once --thread 'Example chat'
python3 -m codex_token_monitor --json --thread 'Example chat' --offline
python3 -m codex_token_monitor --decimals 3
python3 -m codex_token_monitor --refresh 10
python3 -m codex_token_monitor --codex-home /path/to/codex-home
python3 -m codex_token_monitor --help
```

`--thread` accepts an exact title or ID, or a unique substring. Use `--list` to resolve an ambiguous selection. `--refresh` must be a finite number of seconds at least 1; live limit requests remain at least 5 seconds apart. `--once`, `--json`, and `--list` do not require an interactive terminal.

Run the tests from the project directory:

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE).
