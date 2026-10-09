"""The USAGE tab: token usage and what is left of each provider's limit."""
from __future__ import annotations

import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll

from .usage import (WINDOWS, Limit, ProviderUsage, UsageReport, age_text, config_file, money,
                    short_count)

BAR_WIDTH = 20
OK, WARN, HIGH, DIM = "#7ee787", "#f2b84b", "#ff6b7a", "#6b7f8e"


class UsageView(VerticalScroll, can_focus=True):
    BINDINGS = [
        Binding("pageup", "page_up", "Scroll up", show=False),
        Binding("pagedown", "page_down", "Scroll down", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
    ]


def bar(percent: float) -> tuple[str, str]:
    """A fixed-width bar and the colour for how full it is."""
    filled = round(BAR_WIDTH * max(0.0, min(100.0, percent)) / 100)
    colour = HIGH if percent >= 85 else WARN if percent >= 60 else OK
    return "█" * filled + "░" * (BAR_WIDTH - filled), colour


def _limit_line(limit: Limit, now: float) -> Text:
    line = Text("  ")
    line.append(f"{limit.label:<11}", style="bold")
    if limit.used_percent is None:
        line.append("reset; waiting for a new reading", style=DIM)
        return line
    graphic, colour = bar(limit.used_percent)
    line.append(graphic, style=colour)
    line.append(f" {limit.used_percent:5.1f}% used", style=colour)
    line.append(f"  {100 - limit.used_percent:5.1f}% left", style="bold")
    if limit.resets_at:
        line.append(f"  resets in {age_text(limit.resets_at - now)}", style=DIM)
    if limit.as_of and now - limit.as_of > 3600:
        line.append(f"  (read {age_text(now - limit.as_of)} ago)", style=DIM)
    return line


def _token_line(item: ProviderUsage) -> Text:
    line = Text("  ")
    line.append(f"{'tokens':<11}", style="bold")
    for label, _ in WINDOWS:
        tally = item.tallies[label]
        line.append(f"{label} ", style=DIM)
        line.append(f"{short_count(tally.fresh):>6}", style="bold")
        if tally.cost:
            line.append(f" {money(tally.cost)}", style="#d2a8ff")
        line.append("   ")
    cached = item.tallies["7d"].cached
    if cached:
        line.append(f"cached 7d {short_count(cached)}", style=DIM)
    return line


def render_usage(report: UsageReport, now: float | None = None) -> Text:
    now = report.generated if now is None else now
    text = Text()
    text.append("TOKEN USAGE", style="bold #7dcfff")
    text.append(f"   local files only · read {time.strftime('%H:%M:%S', time.localtime(report.generated))}"
                "   (r refreshes)\n\n", style=DIM)
    budget_keys: list[str] = []
    for item in report.providers:
        text.append(item.name.upper() + "\n", style="bold #e0af68")
        for limit in item.limits:
            text.append_text(_limit_line(limit, now))
            text.append("\n")
        if item.tallies:
            text.append_text(_token_line(item))
            text.append("\n")
        if item.key.startswith("opencode:") and not item.limits:
            budget_keys.append(item.key)
        if item.note:
            text.append(f"  {item.note}\n", style=DIM)
        text.append("\n")
    if not report.providers:
        text.append("No usage found.\n", style=DIM)
    if budget_keys:
        text.append("Providers reached through OpenCode report spend but no limit. To see what is left, add\n"
                    f"a budget in {config_file()}, for example:\n"
                    f'  {{"limits": {{"{budget_keys[0]}": {{"7d": {{"usd": 25}}}}}}}}\n'
                    f"keys: {', '.join(budget_keys)}   windows: 5h, 24h, 7d   units: usd, tokens\n", style=DIM)
    return text
