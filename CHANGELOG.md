# Changelog

All notable changes to OC Deck. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **USAGE tab (`7`)**: token usage and what is left, per provider, from local files only. Claude Code's real 5-hour and 7-day plan usage (reported by a status line attached to sessions OC Deck launches; `OCDECK_CLAUDE_USAGE_STATUSLINE=0` turns it off), Codex's stored rate-limit reading, and per-provider tokens and cost for everything run through OpenCode. Optional budgets in `~/.config/ocdeck/usage.json` give providers that report no limit a "left" figure. `python -m ocdeck.usage` prints the same numbers.
- Claude Code permission prompts show as `PERM` in the deck and `y` allows one once (OC Deck-launched sessions carry a `PermissionRequest` hook; the terminal prompt keeps working).
- Claude Code helper agents (spawned with the Agent tool) are tracked as `[Subagent]` children nested under the session that spawned them; `o` on one opens its parent.

### Changed
- Agent browser: optional `"extensions": false` in `agent-browser.json` launches Chrome with `--disable-extensions` (agents use the debugging port only); restarting also drops claude.ai's `artifact=` panel parameter and repeated addresses from the saved tabs. Defaults are unchanged.
- `x x` on a running Claude Code session interrupts the turn with one Esc and keeps the session open, instead of closing the terminal (only after checking the pane runs Claude and shows "esc to interrupt"; a pending permission dialog is never answered by it).
- Agent browser: the tab janitor no longer closes agent chat tabs (those opened with `?agentX=1`), closes ordinary tabs after 15 idle minutes instead of 5, and caps ordinary tabs at 12.
- Agent browser: a memory guard flags (1.5 GB) or reloads (3 GB) heavy agent chat tabs instead of letting them grow; it never closes one.
- Claude Code sessions launched or resumed by OC Deck now start with `--no-chrome`, so
  they never drive your personal Chrome through the Claude in Chrome extension.

## [0.1.0] — first public release

OC Deck had been in daily private use before this release; this is its first
public version.

### Added
- Dashboard for OpenCode (V1 and V2), Claude Code and Codex sessions, grouped by
  project, with live state, runtime/model column and a focus strip.
- Agents board with nested helper agents, background-job display (`◆ JOB`),
  permission and question handling (`y` approves once), and relaunch of
  previously open sessions.
- Harness picker (`Shift+S`) and cross-harness handoff (`Shift+C`); each harness
  is optional.
- Sentinel health layer with privacy-redacted alarms.
- Shared hub and `ocdeck-index` MCP server: one project memory for every harness.
- Optional GNOME integration: Super+O launcher, top-bar button, notification
  click-to-focus, window placement (`desktop/`).
- Optional OpenCode plugins for permission notifications and a read-only API
  bridge (`plugins/`).
- Optional agent browser, a separate persistent Chrome driven over CDP
  (`agent-browser/`).
- A documented contract for plugging in your own project orchestrator
  (`home-agent/`).

### Notes
- Python 3.12+; developed against Ubuntu, GNOME Shell on Wayland, Ptyxis and tmux.
- The history of the private repository this was extracted from is not published.
