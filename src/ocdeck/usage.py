"""Token usage, and what is left of each provider's limit, from local files only.

Nothing here talks to a network or reads a credential. Three kinds of numbers:

* **Limits** the provider itself reported: Claude Code's 5-hour and 7-day usage
  (written by ``ocdeck.usage_statusline`` from the status line Claude Code feeds it)
  and Codex's rate-limit snapshot (stored in every Codex rollout).
* **Tokens** actually spent, summed per window from what each harness logs: Claude Code
  transcripts, Codex rollouts and the OpenCode V2 database (with cost where it keeps one).
* **Budgets** the owner sets in ``~/.config/ocdeck/usage.json`` for providers that report no
  limit of their own (OpenRouter, ...): ``{"limits": {"opencode:openrouter": {"7d": {"usd": 25}}}}``.

"Fresh" tokens are input that was not read from a cache plus output; cached reads are kept
apart because they dominate the count without costing the same. Every reader is bounded and
failure-isolated: an unreadable source becomes a note on its row, never an exception.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

WINDOWS: tuple[tuple[str, int], ...] = (("5h", 5 * 3600), ("24h", 24 * 3600), ("7d", 7 * 86400))
LONGEST = WINDOWS[-1][1]
MAX_FILES = 4000
MAX_LINE_CHARS = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024
SNAPSHOT_NAME = "claude-rate-limits.json"
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "openrouter": "OpenRouter",
                  "opencode": "OpenCode Zen", "opencode-go": "OpenCode Go", "zai-coding-plan": "Z.ai Coding Plan",
                  "google": "Google", "xai": "xAI", "deepseek": "DeepSeek"}


@dataclass(frozen=True, slots=True)
class Tally:
    fresh_input: int = 0
    output: int = 0
    cached: int = 0
    cost: float | None = None  # None: this provider keeps no cost
    messages: int = 0

    @property
    def fresh(self) -> int:
        return self.fresh_input + self.output


@dataclass(frozen=True, slots=True)
class Limit:
    label: str                  # "5h", "7d", "budget 7d"
    used_percent: float | None  # None: the window has reset and nothing newer was read
    resets_at: float | None     # epoch seconds
    source: str                 # "Claude Code status line", "Codex rollout", "budget"
    as_of: float | None = None  # when the provider's figure was read
    stale: bool = False


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    key: str
    name: str
    limits: tuple[Limit, ...] = ()
    tallies: dict[str, Tally] = field(default_factory=dict)  # window label -> Tally
    note: str = ""


@dataclass(frozen=True, slots=True)
class UsageReport:
    generated: float
    providers: tuple[ProviderUsage, ...]


# --- paths ------------------------------------------------------------------

def state_dir() -> Path:
    override = os.environ.get("OCDECK_USAGE_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    return base / "ocdeck" / "usage"


def config_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return base / "ocdeck" / "usage.json"


def claude_projects_dir() -> Path:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    return home / "projects"


def codex_sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "sessions"


def opencode_database() -> Path:
    override = os.environ.get("OCDECK_USAGE_OPENCODE_DB")
    if override:
        return Path(override).expanduser()
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
    return data / "opencode-v2" / "opencode.db"


# --- small helpers ------------------------------------------------------------

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _count(value: Any) -> int:
    number = _number(value)
    return max(0, int(number)) if number is not None else 0


def epoch(value: Any) -> float | None:
    """Epoch seconds from seconds, milliseconds or an ISO-8601 string."""
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e11 else number
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def window_label(minutes: Any) -> str:
    number = _number(minutes)
    if number is None or number <= 0:
        return "limit"
    if number % 1440 == 0:
        return f"{int(number // 1440)}d"
    if number % 60 == 0:
        return f"{int(number // 60)}h"
    return f"{int(number)}m"


class _Sums:
    """Per-window accumulators, filled message by message."""

    def __init__(self) -> None:
        self.fresh_input = {label: 0 for label, _ in WINDOWS}
        self.output = dict(self.fresh_input)
        self.cached = dict(self.fresh_input)
        self.cost = {label: 0.0 for label, _ in WINDOWS}
        self.messages = dict(self.fresh_input)
        self.has_cost = False

    def add(self, when: float, now: float, fresh_input: int, output: int, cached: int,
            cost: float | None = None) -> None:
        for label, seconds in WINDOWS:
            if now - seconds <= when <= now + 300:
                self.fresh_input[label] += fresh_input
                self.output[label] += output
                self.cached[label] += cached
                self.messages[label] += 1
                if cost is not None:
                    self.cost[label] += cost
                    self.has_cost = True

    def tallies(self) -> dict[str, Tally]:
        return {label: Tally(self.fresh_input[label], self.output[label], self.cached[label],
                             self.cost[label] if self.has_cost else None, self.messages[label])
                for label, _ in WINDOWS}


def _recent_files(root: Path, pattern: str, now: float, *, horizon: float = LONGEST) -> list[tuple[Path, os.stat_result]]:
    """Regular files under root newer than the horizon, newest first, bounded."""
    found: list[tuple[Path, os.stat_result]] = []
    stack = [root]
    while stack and len(found) < MAX_FILES * 4:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).match(pattern):
                    info = entry.stat(follow_symlinks=False)
                    if now - info.st_mtime <= horizon:
                        found.append((Path(entry.path), info))
            except OSError:
                continue
    found.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return found[:MAX_FILES]


def _json_lines(path: Path, needle: str):
    """Decoded JSON objects of the lines containing needle (cheap prefilter first)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if needle not in line or len(line) > MAX_LINE_CHARS:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


# --- Claude Code ----------------------------------------------------------------

# path -> (mtime_ns, size, {message id: (when, fresh_input, output, cached)})
_claude_cache: dict[str, tuple[int, int, dict[str, tuple[float, int, int, int]]]] = {}


def _claude_messages(path: Path, info: os.stat_result) -> dict[str, tuple[float, int, int, int]]:
    cached = _claude_cache.get(str(path))
    if cached and cached[0] == info.st_mtime_ns and cached[1] == info.st_size:
        return cached[2]
    messages: dict[str, tuple[float, int, int, int]] = {}
    for entry in _json_lines(path, '"usage"'):
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        when = epoch(entry.get("timestamp"))
        if not isinstance(usage, dict) or when is None:
            continue
        identifier = str(message.get("id") or entry.get("requestId") or entry.get("uuid") or "")
        if not identifier:
            continue
        row = (when, _count(usage.get("input_tokens")) + _count(usage.get("cache_creation_input_tokens")),
               _count(usage.get("output_tokens")), _count(usage.get("cache_read_input_tokens")))
        previous = messages.get(identifier)
        if previous is None or row[2] >= previous[2]:  # streaming writes the same message repeatedly
            messages[identifier] = row
    if len(_claude_cache) > MAX_FILES * 2:
        _claude_cache.clear()
    _claude_cache[str(path)] = (info.st_mtime_ns, info.st_size, messages)
    return messages


def claude_limits(now: float, directory: Path | None = None) -> tuple[Limit, ...]:
    """The 5-hour / 7-day usage Claude Code last reported to ocdeck.usage_statusline."""
    path = (directory or state_dir()) / SNAPSHOT_NAME
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return ()
    captured = _number(data.get("captured"))
    result = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        used, resets = _number(item.get("used_percentage")), epoch(item.get("resets_at"))
        if used is None:
            continue
        expired = resets is not None and resets <= now
        result.append(Limit(label, None if expired else max(0.0, min(100.0, used)), resets,
                            "Claude Code status line", captured, stale=expired))
    return tuple(result)


def collect_claude(now: float, root: Path | None = None, directory: Path | None = None) -> ProviderUsage:
    sums, merged = _Sums(), {}
    root = root or claude_projects_dir()
    for path, info in _recent_files(root, "*.jsonl", now):
        for identifier, row in _claude_messages(path, info).items():
            known = merged.get(identifier)
            if known is None or row[2] > known[2]:  # a resumed session repeats earlier messages
                merged[identifier] = row
    for when, fresh_input, output, cached in merged.values():
        sums.add(when, now, fresh_input, output, cached)
    limits = claude_limits(now, directory)
    note = "" if limits else ("no limit reading yet: start a Claude session from OC Deck "
                              "(its status line reports the 5h / 7d usage)")
    return ProviderUsage("claude-code", "Anthropic · Claude Code", limits, sums.tallies(), note)


# --- Codex ----------------------------------------------------------------------

# path -> (mtime_ns, size, events, newest rate_limits as (when, dict) | None)
_codex_cache: dict[str, tuple[int, int, list[tuple[float, int, int, int]], tuple[float, dict] | None]] = {}


def _codex_file(path: Path, info: os.stat_result):
    cached = _codex_cache.get(str(path))
    if cached and cached[0] == info.st_mtime_ns and cached[1] == info.st_size:
        return cached[2], cached[3]
    events: list[tuple[float, int, int, int]] = []
    newest: tuple[float, dict] | None = None
    previous_total = None
    for entry in _json_lines(path, '"token_count"'):
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        when = epoch(entry.get("timestamp"))
        if when is None:
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict) and (newest is None or when >= newest[0]):
            newest = (when, limits)
        info_block = payload.get("info")
        last = info_block.get("last_token_usage") if isinstance(info_block, dict) else None
        total = info_block.get("total_token_usage") if isinstance(info_block, dict) else None
        if not isinstance(last, dict) or total == previous_total:  # the same total is reported twice
            continue
        previous_total = total
        cached_input = _count(last.get("cached_input_tokens"))
        events.append((when, max(0, _count(last.get("input_tokens")) - cached_input),
                       _count(last.get("output_tokens")), cached_input))
    if len(_codex_cache) > MAX_FILES * 2:
        _codex_cache.clear()
    _codex_cache[str(path)] = (info.st_mtime_ns, info.st_size, events, newest)
    return events, newest


def collect_codex(now: float, root: Path | None = None) -> ProviderUsage:
    root = root or codex_sessions_dir()
    sums, newest = _Sums(), None
    for path, info in _recent_files(root, "rollout-*.jsonl", now):
        events, snapshot = _codex_file(path, info)
        for when, fresh_input, output, cached in events:
            sums.add(when, now, fresh_input, output, cached)
        if snapshot and (newest is None or snapshot[0] > newest[0]):
            newest = snapshot
    if newest is None:  # nothing recent: the newest older rollout still has the last reading
        for path, info in _recent_files(root, "rollout-*.jsonl", now, horizon=365 * 86400)[:12]:
            snapshot = _codex_file(path, info)[1]
            if snapshot and (newest is None or snapshot[0] > newest[0]):
                newest = snapshot
    limits: list[Limit] = []
    note = "no Codex rate-limit reading found"
    if newest:
        when, data = newest
        for key in ("primary", "secondary"):
            item = data.get(key)
            if not isinstance(item, dict):
                continue
            used = _number(item.get("used_percent"))
            resets = epoch(item.get("resets_at"))
            if used is None:
                continue
            expired = resets is not None and resets <= now
            limits.append(Limit(window_label(item.get("window_minutes")),
                                None if expired else max(0.0, min(100.0, used)), resets,
                                "Codex rollout", when, stale=expired))
        plan = data.get("plan_type")
        note = f"plan {plan}" if isinstance(plan, str) and plan else ""
        if limits and now - when > 6 * 3600:
            note = (note + " · " if note else "") + "reading is " + age_text(now - when) + " old"
    return ProviderUsage("codex", "OpenAI · Codex", tuple(limits), sums.tallies(), note)


# --- OpenCode --------------------------------------------------------------------

BUCKET_MS = 300_000          # sums are kept per provider per 5 minutes
REREAD_SECONDS = 3 * 3600    # a message's tokens are written when it finishes: re-read the recent past
# database path -> (read up to, {(provider, bucket): [fresh, output, cached, cost, messages]})
_opencode_state: dict[str, tuple[int, dict[tuple[str, int], list[float]]]] = {}

_OPENCODE_SQL = (
    "SELECT json_extract(data,'$.model.providerID') AS provider, time_created / :bucket AS bucket,"
    " SUM(COALESCE(json_extract(data,'$.tokens.input'),0) + COALESCE(json_extract(data,'$.tokens.cache.write'),0)),"
    " SUM(COALESCE(json_extract(data,'$.tokens.output'),0) + COALESCE(json_extract(data,'$.tokens.reasoning'),0)),"
    " SUM(COALESCE(json_extract(data,'$.tokens.cache.read'),0)),"
    " SUM(COALESCE(json_extract(data,'$.cost'),0)), COUNT(*)"
    " FROM session_message WHERE type IN ('assistant','compaction') AND time_created >= :since"
    " GROUP BY provider, bucket"
)


def _opencode_buckets(path: Path, now: float) -> dict[tuple[str, int], list[float]]:
    """Per-provider 5-minute sums for the last 7 days; later calls re-read only the recent past."""
    now_ms = int(now * 1000)
    horizon = now_ms - LONGEST * 1000
    known = _opencode_state.get(str(path))
    if known is None or known[0] < horizon:
        since, kept = horizon, {}
    else:
        since = max(horizon, known[0] - REREAD_SECONDS * 1000)
        kept = known[1]
    since_bucket = since // BUCKET_MS
    buckets = {key: value for key, value in kept.items() if horizon // BUCKET_MS <= key[1] < since_bucket}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=1")
        rows = connection.execute(_OPENCODE_SQL, {"bucket": BUCKET_MS, "since": since_bucket * BUCKET_MS}).fetchall()
    finally:
        connection.close()
    for provider, bucket, fresh, output, cached, cost, count in rows:
        buckets[(str(provider or "unknown"), int(bucket))] = [
            _count(fresh), _count(output), _count(cached), float(cost or 0.0), _count(count)]
    if len(_opencode_state) > 8:
        _opencode_state.clear()
    _opencode_state[str(path)] = (now_ms, buckets)
    return buckets


def collect_opencode(now: float, database: Path | None = None) -> list[ProviderUsage]:
    path = database or opencode_database()
    try:
        buckets = _opencode_buckets(path, now)
    except sqlite3.OperationalError as error:
        text = str(error)
        kind = "database unreadable" if "unable to open" in text else "usage query failed"
        return [ProviderUsage("opencode", "OpenCode", note=f"{kind} ({text})")]
    except sqlite3.Error as error:
        return [ProviderUsage("opencode", "OpenCode", note=f"usage query failed ({error})")]
    now_ms = int(now * 1000)
    totals: dict[str, dict[str, list[float]]] = {}
    for (provider, bucket), values in buckets.items():
        start = bucket * BUCKET_MS
        for label, seconds in WINDOWS:
            if now_ms - seconds * 1000 <= start <= now_ms + 300_000:
                sums = totals.setdefault(provider, {name: [0, 0, 0, 0.0, 0] for name, _ in WINDOWS})[label]
                for index, value in enumerate(values):
                    sums[index] += value
    result = []
    for provider, windows in totals.items():
        tallies = {label: Tally(int(v[0]), int(v[1]), int(v[2]), float(v[3]), int(v[4])) for label, v in windows.items()}
        if not any(t.fresh or t.cached or t.cost for t in tallies.values()):
            continue  # only failed calls: nothing was spent
        name = PROVIDER_NAMES.get(provider, provider.title()) + " · via OpenCode"
        result.append(ProviderUsage(f"opencode:{provider}", name, (), tallies))
    result.sort(key=lambda item: item.tallies["7d"].fresh, reverse=True)
    return result


# --- budgets and the whole report -------------------------------------------------------

def read_budgets(path: Path | None = None) -> dict[str, dict[str, dict[str, float]]]:
    path = path or config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size < 64 * 1024 else {}
    except (OSError, ValueError):
        return {}
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return {}
    result: dict[str, dict[str, dict[str, float]]] = {}
    for key, windows in limits.items():
        if not isinstance(windows, dict):
            continue
        for label, spec in windows.items():
            if label not in dict(WINDOWS) or not isinstance(spec, dict):
                continue
            kept = {unit: _number(spec.get(unit)) for unit in ("usd", "tokens")}
            kept = {unit: value for unit, value in kept.items() if value and value > 0}
            if kept:
                result.setdefault(str(key), {})[label] = kept
    return result


def _with_budgets(item: ProviderUsage, budgets: dict[str, dict[str, dict[str, float]]]) -> ProviderUsage:
    added = []
    have = {limit.label for limit in item.limits}
    for label, spec in budgets.get(item.key, {}).items():
        tally = item.tallies.get(label)
        if tally is None or label in have:
            continue
        shares = []
        if "usd" in spec and tally.cost is not None:
            shares.append(tally.cost / spec["usd"] * 100)
        if "tokens" in spec:
            shares.append(tally.fresh / spec["tokens"] * 100)
        if shares:
            added.append(Limit(f"budget {label}", min(100.0, max(shares)), None, "budget"))
    if not added:
        return item
    return ProviderUsage(item.key, item.name, item.limits + tuple(added), item.tallies, item.note)


def collect_usage(now: float | None = None, *, claude_root: Path | None = None,
                  codex_root: Path | None = None, opencode_db: Path | None = None,
                  directory: Path | None = None, budgets_file: Path | None = None) -> UsageReport:
    """Every provider's usage; one broken source never hides the others."""
    now = time.time() if now is None else now
    providers: list[ProviderUsage] = []
    steps: list[tuple[str, str, Callable[[], list[ProviderUsage]]]] = [
        ("claude-code", "Anthropic · Claude Code", lambda: [collect_claude(now, claude_root, directory)]),
        ("codex", "OpenAI · Codex", lambda: [collect_codex(now, codex_root)]),
        ("opencode", "OpenCode", lambda: collect_opencode(now, opencode_db)),
    ]
    for key, name, run in steps:
        try:
            providers.extend(run())
        except Exception as error:  # a reader bug must not blank the whole tab
            providers.append(ProviderUsage(key, name, note=f"could not read ({type(error).__name__})"))
    budgets = read_budgets(budgets_file)
    return UsageReport(now, tuple(_with_budgets(item, budgets) for item in providers))


# --- text ----------------------------------------------------------------------

def age_text(seconds: float) -> str:
    """``3d 4h``, ``2h 10m``, ``45m``; under a minute is ``<1m``."""
    days, rest = divmod(max(0, int(seconds)), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


def short_count(value: float) -> str:
    value = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            text = f"{value / limit:.1f}".rstrip("0").rstrip(".")
            return text + suffix
    return str(int(value))


def money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def headline(report: UsageReport) -> str:
    """One short line: the real limits only, e.g. ``cc 5h 12% 7d 41% · codex 7d 28%``."""
    short = {"claude-code": "cc", "codex": "codex"}
    parts = []
    for item in report.providers:
        shown = [f"{limit.label} {limit.used_percent:.0f}%" for limit in item.limits
                 if limit.used_percent is not None and not limit.stale and limit.source != "budget"]
        if shown and item.key in short:
            parts.append(f"{short[item.key]} " + " ".join(shown))
    return " · ".join(parts)


def format_report(report: UsageReport) -> str:
    """Plain-text table, also what ``python -m ocdeck.usage`` prints."""
    now, lines = report.generated, []
    for item in report.providers:
        lines.append(item.name.upper())
        for limit in item.limits:
            if limit.used_percent is None:
                lines.append(f"  {limit.label:<10} reset, no newer reading   ({limit.source})")
                continue
            reset = f"resets in {age_text(limit.resets_at - now)}" if limit.resets_at else ""
            lines.append(f"  {limit.label:<10} {limit.used_percent:5.1f}% used  {100 - limit.used_percent:5.1f}% left  "
                         f"{reset}  ({limit.source})".rstrip())
        if item.tallies:
            cells = []
            for label, _ in WINDOWS:
                tally = item.tallies[label]
                cost = f" {money(tally.cost)}" if tally.cost else ""
                cells.append(f"{label} {short_count(tally.fresh)}{cost}")
            lines.append("  tokens     " + "   ".join(cells) +
                         f"   (cached 7d {short_count(item.tallies['7d'].cached)})")
        if item.note:
            lines.append(f"  note       {item.note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    print(format_report(collect_usage()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
