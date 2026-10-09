# OC Deck reference

This is the detailed reference. Start with the [README](../README.md).
Home Agent is an optional integration: see [home-agent/README.md](../home-agent/README.md).


OC Deck is a terminal-native operations console for OpenCode. It provides a
compact view of projects, sessions, local services, and machine health. It uses
the saved backend selection for both terminal and desktop launches. OpenCode V2
is the default on an unconfigured installation; V1 remains available as an
explicit temporary rollback mode.


## Install

See the main [README](../README.md#install).

## Run

```bash
ocdeck
```

Plain `ocdeck` honors the selection saved by `ocdeck-backend v1` or
`ocdeck-backend v2`, just like the desktop launcher. For V2, OC Deck invokes
`opencode2 api` with V2 OpenAPI operation IDs, so the OpenCode CLI owns shared-service
discovery and authentication. It does not read V2's service registration or
credentials. Use the rollback backend only when needed:

```bash
ocdeck --backend v1
```

For direct CLI invocation the precedence is `--backend`, then
`OCDECK_OPENCODE_BACKEND`, then the saved selector, then V2 if no selector exists.
The selector uses `$XDG_CONFIG_HOME/ocdeck/backend` (normally
`~/.config/ocdeck/backend`), or `OCDECK_BACKEND_FILE` when explicitly set.
Invalid saved selection is reported rather than silently switching backends;
an explicit `--backend` remains available to recover.

Backend selection is deterministic; OC Deck never chooses a backend by probing
which executables happen to be installed and never falls from a failed V2
request into V1. `--url` selects an explicit API server; on V2, the same
`--server` value is passed to every TUI launch so sessions keep their server
affinity. Set `OCDECK_OPENCODE_BACKEND=v1` or `v2` to pin the validated default
for a direct invocation or custom service; an explicit `--backend` always wins.
The managed launchers use the persistent selector below instead.

Desktop, hotkey, and GNOME launchers use `ocdeck-entrypoint` and one
persistent owner-only selector. Initialize every future entrypoint launch for
V2, or switch it to the V1 rollback, without replacing files or restoring
credentials:

```bash
ocdeck-backend v2
ocdeck-backend v1
```

Return them to V2 with `ocdeck-backend v2`. V2 entrypoint launches require the
`opencode2.service` when one is installed; V1 launches use `opencode-web.service` when installed. With no managed service OC Deck calls the `opencode`/`opencode2` command directly. Both
paths remove inherited OpenCode server variables, while V1 reads its owner-only
credential file internally. Missing, public, symlinked, or malformed selector
state fails closed. The permission watcher service is restarted so they adopt the new selection; already-open desktop
dashboards keep their current backend until closed. Every active service
restart is attempted, and all selector-aware services are stopped if any
restart fails.

Use `ocdeck --once` for a noninteractive report.

The optional desktop notification watcher also uses the persistent selector.
Initialize it for V2 before starting the managed service:

```bash
ocdeck-backend v2
ocdeck-permission-watcher
```

In V2 it discovers and authenticates through `opencode2 api`, polls every loaded V2
location (including distinct workspaces in the same directory) for permission
and form requests, and never reads the V1 credential or V1 plugin-state files.
Use `ocdeck-permission-watcher --backend v1` for a direct rollback invocation,
or `ocdeck-backend v1` to switch the managed service with the other entrypoints.
The tracked `systemd/ocdeck-permission-watcher.service` has no unconditional
dependency on either server; the shared entrypoint starts gated V2 only when V2
is selected. Installing the template does not modify a live user unit
automatically.

The watcher is desktop recovery/notification only. It does not approve or
forward anything; it avoids duplicating notifications that a plugin already
published for the same request.

Failed desktop delivery is retried instead of being recorded as delivered.
Temporary or partial API failures retain deduplication history, preventing old
pending requests from generating duplicate banners when the API recovers.

Use `ocdeck --inline-tmux` in a web terminal. Opening a session temporarily
suspends the dashboard and attaches to its tmux session in the same terminal;
detaching or exiting restores OC Deck. The selected target is written to
`$XDG_RUNTIME_DIR/ocdeck-mobile-target.json` for reviewed phone dictation.

Use `ocdeck --briefings-file PATH` to read a different Home Agent briefing
artifact for the `NEXT` view.

## Top-bar launcher

The `OC Deck` status indicator opens the dashboard in a new Ptyxis window.
Click the indicator and choose **Open OC Deck**, or middle-click it for a quick
launch. If Hide Top Bar is enabled, move the pointer to the top edge first.
Caps Lock emits Super on the configured keyboards, so Caps Lock + `O` launches
OC Deck when absent and cycles through existing OC Deck windows. It selects the
Deck tab before verifying actual GNOME focus, including in a multi-tab Ptyxis
window. Both launchers use standalone Ptyxis instances; a terminal that contains
other tabs can remain open after Deck exits.

```bash
systemctl --user status ocdeck-indicator.service
systemctl --user restart ocdeck-indicator.service
```

The project list also reads the `Project` and `Code` columns from the Markdown
catalog at `~/.config/home-agent/projects.md`.
Relative code paths are resolved from the vault root. Set
`OCDECK_PROJECTS_FILE` or pass `--projects-file PATH` to use another catalog.
The catalog is reread on every refresh, so projects appear even before they
have an OpenCode session. Use `new-project "Name" /absolute/code/path [host]`
or `home-agentctl register-project ...` instead of editing only the JSON
registry; successful registration updates both sources before returning.
OC Deck also reads `~/.config/home-agent/registry.json` and retains every valid
registry identity if a Google Drive rewrite temporarily omits its Markdown row.
The periodic Home Agent monitor restores such rows under the authority lock;
metadata conflicts fail closed.

The directory picker and pasted-path registration compare exact project
directories. A subdirectory of an existing project can be registered separately,
and registered projects remain visible even before their first session.
Only catalog/registry-backed entries count as already registered; a directory
discovered from OpenCode session history still goes through registration.

Historical sessions launched from generic directories are assigned by exact
session ID using `Projects/_session-routes.json`. Set
`OCDECK_SESSION_ROUTES_FILE` or pass `--session-routes-file PATH` to override
it. Subagent sessions inherit their parent task's routed project. Routing
changes OC Deck's grouping only; it does not modify OpenCode data. Reopening a
routed session starts it from the canonical project root instead of its old
generic directory.

The `NEXT` view reads Home Agent's latest briefing from
`$XDG_STATE_HOME/home-agent/reports/latest.json`. Set
`OCDECK_BRIEFINGS_FILE` or pass `--briefings-file PATH` to override it. OC Deck
supports schema version `1` and joins report entries to dashboard projects only
when their normalized `projectPath` values are exactly equal. It never guesses
from `projectID`, names, or parent directories. Missing, oversized, unsupported,
or malformed artifacts are ignored without failing the rest of the dashboard
refresh. A malformed listed project invalidates the whole artifact; valid
projects with unknown paths are filtered only during exact-path matching.

Sessions are grouped by their project directory: a session appears under the
project whose worktree contains its directory, and directories outside any
known worktree (e.g. registered sandboxes) become their own projects.

The Operations list defaults to **Main sessions**. Native subagents and sessions
explicitly marked as automated Home Agent workers, reporters, or monitors are
hidden, not deleted. Interactive voice sessions and roots with unknown provenance
remain visible; titles and model names are never used to guess who created a
session. Check **Include agent sessions** below the list, or press `b`, to show
the full history with orange `[Subagent]`, `[Worker]`, `[Reporter]`, and `[Monitor]`
labels. The session metric and project session counts follow this choice.

Active search and project filters are displayed above the results. **Clear
filters** removes both search terms and the project scope without changing the
agent-session visibility choice. For example, searching `ma` excludes a session
named `agents_game`; clear the search or search for `agents_game` instead.
The live Agents board, permission alerts, and mobile live-terminal list always
retain agent-created sessions. `g` reveals a pending permission even when its
session was hidden by these filters.

## Harnesses: OpenCode, Claude Code and Codex

OC Deck shows sessions from every enabled agent CLI in one place, tagged by
harness, and launches or resumes them in tmux the same way. Every harness is
optional, OpenCode included: disable any of them and the deck keeps working
with the rest.

| Setting | Meaning |
| --- | --- |
| `~/.config/ocdeck/harnesses.json` | `{"opencode": "auto", "claude": "auto", "codex": "auto"}`; each value is `on`, `off` or `auto` (enabled when the CLI is installed) |
| `ocdeck --harness claude,codex` or `OCDECK_HARNESSES=…` | Override the settings for one run |
| `ocdeck-hub harness off opencode` | Change the saved setting |

Full refreshes (`r` or the regular refresh interval) recheck installed CLIs
and saved settings. Newly installed harnesses appear without restarting the
deck; existing adapter caches and the selected launch harness are retained.
CLI/environment overrides stay pinned for that run.

- **Your own Chrome stays out of it.** Every Claude Code session OC Deck launches
  or resumes carries `--no-chrome`, so the Claude in Chrome extension never drives
  your personal browser, whatever your global Claude setting says. A session you
  grant the agent browser (`Shift+B`) reaches the dedicated agent browser instead.
- **Sessions.** Claude Code sessions come from `~/.claude/projects/*/*.jsonl`;
  Codex sessions from `~/.codex/sessions/**/rollout-*.jsonl`. Sessions are
  joined to projects by directory, and the detail pane shows each project's
  git branch and number of changed files.
- **Helper agents.** When a Claude Code session spawns agents with its Agent
  tool, their transcripts (`<session>/subagents/agent-<id>.jsonl`, titled from the
  matching `.meta.json`) appear as `[Subagent]` children nested under that
  session, like OpenCode's child sessions. One shows `RUN` only while its
  parent CLI is alive and its own transcript is mid-turn and was written in the
  last two minutes; otherwise it is a finished child. They have no terminal:
  `o` on one opens the session that spawned it. At most the 100 newest are read,
  and only those whose parent session is listed. Codex helper agents are not
  tracked yet.
- **Agents board.** The RUNTIME column shows harness and model: short codes
  on narrow windows (`CC OP5`, `OC AST`, `CX CDX`) and full names on wide ones
  (`Claude Code · claude-opus-5-5`). STATE uses `RUN`, `PERM`, `ASK`, `RTRY`,
  `STAL`, `REV`, `IDLE`, and `CLSD`. TERM uses `OPN` (open window), `TMX`
  (background tmux), `DIR` (direct terminal), `SRV` (server), and `SAV`
  (saved). DETAIL hides first as the window narrows; PROJECT also hides in
  very small windows. The focus strip keeps the full title, prompt, runtime,
  and expanded state/terminal descriptions. Resizing preserves selection.
  A leading `↳` in STATE (for example `↳ RUN`) means the activity comes from
  a nested helper; the focus strip shows the parent's own state alongside
  the helper's state, title and runtime. Press Left to expand the group.
- **Claude status.** Local slash-command output does not start a model turn.
  Completed, interrupted and quota-error turns stop showing RUN; real prompts,
  replies and background notifications can start another turn. Synthetic CLI
  notices preserve the last observed real model (for example `CC SN5`).
- **Terminal headers.** Managed terminals get a uniform top header with
  harness, model, project and title at launch. Full refreshes update live
  managed headers off the UI thread, including renamed sessions. Text is
  placed in tmux user options, while formats contain only fixed references.
  Privacy mode redacts project, title and model in these headers too.
- **Codex approvals.** A pending native approval shows **PERMISSION** (`PERM`
  in compact layouts). Press `g` to select it, then `o` to open Codex and
  respond there. Status comes from the existing local Codex daemon; if it is
  unavailable, OC Deck falls back to transcript turn activity. Transcript-only
  fallback cannot detect approval prompts. Runtime reads do not grant permission.
- **Open and resume.** `o` on a Claude or Codex session attaches its OC
  Deck tmux terminal (`cc-<id>` / `cx-<id>`), focuses the window of a session
  running directly in a terminal tab, or resumes a closed one with
  `claude --resume` / `codex resume`. OC Deck never starts a second process on a
  session that is already running.
- **Agent browser.** `Shift+B` grants or revokes the signed-in agent browser
  for one Claude or Codex session. Only granted sessions are launched with it,
  through `--mcp-config` (Claude) or `-c mcp_servers…` (Codex); no global
  configuration changes. The grant file only records the operator's choice. It
  is not a security boundary.
- **Claude Code permissions.** A Claude session that OC Deck launches or
  resumes carries a `PermissionRequest` hook (`--settings`, file
  `~/.config/ocdeck/claude-permission-hook.json`). When Claude asks to run
  something, the row shows `PERM` with what it wants, and `y` allows it once,
  exactly like OpenCode. Claude's own prompt still appears in the terminal at the
  same time; answering there works too and clears the `PERM` within a second.
  The deck never denies and never allows "always". If nobody answers in 90
  seconds (`OCDECK_PERMISSION_WAIT`) the hook steps aside and the terminal prompt
  stays. Sessions started outside OC Deck have no hook, so they show no `PERM`.
  Set `OCDECK_CLAUDE_PERMISSION_HOOK=0` to launch without it. The same file also
  attaches a status line that reports the plan's 5-hour and 7-day usage to the
  **USAGE** tab (`OCDECK_CLAUDE_USAGE_STATUSLINE=0` turns that off).
- **OpenCode-only actions.** Renaming works only for OpenCode sessions, and
  approving permissions (`y`) only for OpenCode and Claude Code. On other
  harnesses OC Deck says so instead of acting.

### Choosing a harness and agent

**Choose a harness and agent for this project:** select a project or session
and press **Shift+S**, or click **HARNESS / AGENT** on the Agents board.
Choose an enabled harness and its agent (OpenCode/Claude) or profile (Codex).
**New** starts a fresh conversation in that project. **Continue** starts the
chosen harness with handoff notes from the selected session; the original
session keeps its identity. Escape cancels without launching anything.
Existing `n`, Shift+H and Shift+C shortcuts remain available.

For a session displayed in the **Scratch** group, new sessions, handoffs and
project-local agent discovery use that session's actual working directory;
the display grouping does not redirect work to the common temporary root.

The picker discovers visible primary OpenCode Markdown agents, Claude
Markdown agents by their frontmatter `name`, and Codex `<name>.config.toml`
profile filenames. Choose **Other configured name…** for configuration-only
or plugin-provided agents. The native harness validates and loads the selected
configuration; the picker does not change permission flags or activate shared
memory registration. Codex profiles follow the
[official profile configuration](https://learn.chatgpt.com/docs/config-file/config-basic).
Agent flags follow the [OpenCode agent](https://opencode.ai/v2/docs/agents) and
[Claude session-wide agent](https://code.claude.com/docs/en/sub-agents) contracts.

### Adding a harness

Each harness is described once, by a `HarnessSpec` in the registry
(`src/ocdeck/harnesses.py`). Everything else derives from it: badges, the
RUNTIME code, tmux names (`<prefix>-<id>`), `z z` purging, Shift+L history,
lineage nesting, handoff and `ocdeck-hub status`. To add one, such as Gemini
CLI or Aider:

1. Copy `src/ocdeck/harness_plugins/_template.py` to
   `src/ocdeck/harness_plugins/<id>.py`.
2. Fill in the adapter: where transcripts live, how to parse one, and how to
   start or resume a session. Then register the spec: a unique id, a
   two-letter code and a tmux prefix.
3. Add `tests/test_harness_<id>.py` with a small transcript fixture.
4. Optional: add a config-sync planner to `ocdeck.hub.SYNC_PLANNERS`.

Plugins are loaded only from that in-tree folder, never from a path named
in a config file. A config-named module would let anything that can edit
the config run code inside OC Deck. A plugin that fails to import is
skipped with a warning.

### Shared layer: `ocdeck-hub` and `ocdeck-index`

- `ocdeck-hub init` creates `~/.config/agents/hub.json`: the shared
  instructions file and the MCP servers every harness should have. `ocdeck-hub
  status` and `ocdeck-hub sync` show the plan; `ocdeck-hub sync --apply` writes
  it, backing each file up once to `<file>.ocdeck-bak`. Only enabled harnesses
  are touched. The signed-in browser servers are never synced globally.
  A protected, root-owned OpenCode configuration is never written; the hub
  prints the manual steps instead.
- `ocdeck-index` is a stdio MCP server that any harness can use: an
  incremental full-text index of a project, file listing, git status and a
  shared memory store under `~/.config/agents`.
- `ocdeck-hub handoff <session>` (or `Shift+C` in the deck) writes handoff
  notes (recent requests, the last reply, git state) and starts the target
  harness on them.

## Keys

| Key | Action |
| --- | --- |
| `1` | Operations overview |
| `2` | Service health |
| `3` | Key reference |
| `4` | Live agents board |
| `5` | Portfolio briefing and next steps |
| `6` | Sentinel health and global alarms |
| `7` | Token usage and what is left, per provider |
| `Ctrl+Left` / `Ctrl+Right` | Previous or next view |
| `Tab` / `Shift+Tab` | Move focus through controls |
| `Up` / `Down` or `j` / `k` | Move through rows |
| `Left` / `Right` or `h` / `l` | Move between project and session panes |
| `Left` in AGENTS | Expand/collapse live subagents beneath the selected agent |
| `/` | Search all sessions; scoped-project matches appear first |
| `r` | Refresh |
| `d` | Focus the directory field and register an existing absolute directory as a project |
| `p` | Toggle privacy mode |
| `o` or `Enter` | Attach to the selected session's terminal; on a CLOSED agent row, relaunch it |
| `Enter` in ALARMS | Locate the exact known session in Operations |
| `Shift+L` in AGENTS | Relaunch all previously open agent sessions (press twice to confirm) |
| `z` in AGENTS | Close every attached agent terminal window and leave its tmux session running in the background (press twice to confirm); a window that also hosts other tabs is detached but never closed |
| `y` | Approve the selected pending permission once (OpenCode and Claude Code) |
| `g` | Jump to the first pending permission |
| Click a session's name | Rename it; `Enter` saves, `Esc` cancels |
| `a` / **Auto** | New/resume with `--auto`; server-attached browser sessions reopen with their saved server permissions |
| `x` | Close the selected tmux job or exact direct terminal (press twice to confirm); history is retained |
| `n` or **+ NEW SESSION** | Start a session in the selected project |
| `Shift+N` or **+ BROWSER SESSION** | Start a blank browser-enabled session; choose the model with `/models` |
| `Shift+B` or **ENABLE BROWSER** | Grant the selected eligible idle primary browser access, preserving its model |
| `t` | Open a fresh shell terminal in the selected project |
| `Shift+H` | Cycle the harness for new sessions (OpenCode / Claude Code / Codex, among the enabled ones) |
| `Shift+S` | Choose harness and agent/profile in the selected project; start fresh or continue with handoff notes |
| `Shift+C` | Hand the selected session off to the `Shift+H` harness, with notes and git state |
| `f` | Scope the session list to the selected project |
| `b` | Include/hide agent-created sessions in the Operations list |
| `m` | Minimize the window; OC Deck keeps running in the background |
| `q` | Quit |

## Signed-in browser sessions and model choice

The default agent browser is a **separate persistent Chrome profile** (see
[`agent-browser/`](../agent-browser/README.md); the display it opens on is configurable). Both browser MCP tool names connect to that browser.
Sign in to your sites once there; the profile keeps its own logins. Start/show it
with `oc_agent_browser`. It opens on the laptop and stays wherever you drag it —
agents only drive tabs and never move the window.

In OC Deck, select a project and press **Shift+N** to start a blank browser-enabled
session. Choose Astra, Sol, or another configured model in OpenCode with
**`/models`**. There is no model override or automatic first prompt. Model changes
after a browser grant keep the grant; its recorded model is launch provenance,
not a model lock.

The project list also includes directories discovered from session history, such
as your home folder; appearing in that list does not register a managed project.
**Shift+N** can create an ordinary browser-enabled session in a discovered folder
when that folder's effective OpenCode configuration explicitly allows
`signed_in_tabs_*` and its configured signed-in MCP is connected. This native
path does not add the folder to the catalog or make the session a Home Agent
worker. Without that explicit native permission, register the exact directory
first to use the existing scoped browser-grant workflow.

To enable an existing session, select its row and press **Shift+B**, then
**Enter** to open its browser-connected terminal. The session must be an idle
primary in its registered project. If the session is already running in another
terminal — including one started outside OC Deck, such as a tmux session or an
editor's integrated terminal — **Open** focuses that live terminal instead of
starting a browser terminal. A managed browser terminal whose pane is occupied
by something else (a stale viewer, for example) is detected and replaced rather
than attached. With the free
browser and trusted-internal-resource settings enabled, OpenCode allows browser interactions and
local tools across project directories. User-granted sessions remain visible
in **Main sessions**.

### From any terminal: `oc_agent_web`

```bash
# In a registered project directory (or one of its subdirectories):
oc_agent_web

# From any other terminal, specify the project directory:
oc_agent_web /path/to/project

# Enable and open an existing session, preserving history and model:
oc_agent_web --session ses_<id>

# Select a ChatGPT page for the existing agent's next browser task:
oc_agent_web --session ses_<id> --url https://chatgpt.com/c/<conversation-id>

# Select an explicit OpenCode API server:
oc_agent_web --session ses_<id> --api-url http://127.0.0.1:4096
```

This command creates a blank session by default and opens it in the **current
terminal**. It selects the nearest registered code root for a nested working
directory. Use `new-project` or OC Deck's directory registration for a new project.
`--session` reuses the exact supplied idle primary and discovers its saved
directory automatically, even when your terminal is inside another registered
project. An explicit positional directory must match that session's project.
`--url` records a browser page as context for the next task using a no-reply
message; a ChatGPT link is never used as an authenticated OpenCode API address.
`--api-url` selects an explicit V1 API endpoint. The legacy `--url` spelling is
also accepted for a loopback API origin such as `http://127.0.0.1:4096`.
The launcher preserves the existing model and starts no model generation; use
`/models` whenever you want to switch models.

Browser sessions attach to the V1 backend where the signed-in MCP was registered,
so they use that connection immediately. Credentials are passed only in the
native client's environment, never terminal command arguments. The launcher
does not restart the backend or Chrome. V2 does not yet expose this grant path;
OC Deck reports that limitation rather than changing backends.

Uncertain operations are inspected rather than retried automatically. If creating
a terminal fails after session creation, the session is retained and can be
reopened by its ID. Reopen OC Deck to load updated UI shortcuts; `oc_agent_web`
uses the current installed source immediately.

## Selection and terminal behavior

Highlighting a project scopes the session list to that project; press `f` to
release the scope across projects, retaining the agent-session visibility choice.
`Enter` on a project moves into
its session list. The cyan pane border shows where keyboard input is active.
While search text is present, matching sessions from the scoped project appear
first, followed by keyword matches from every other project. Clearing the search
restores the strict project-only scope.
Periodic refresh updates existing table cells in place. When rows are added,
removed, or reordered, selection is restored by project/session ID; refresh
events do not act as keyboard or mouse navigation. Hover and scroll state are
retained so an idle pointer does not flash a highlight onto the first row.
In the `NEXT` view, `Up` / `Down` or `j` / `k` cycles the same selected project
without changing the session scope; `PageUp` / `PageDown` scrolls long reports.

The **+ NEW SESSION** button and resume/new-session actions run the selected
OpenCode backend in a named tmux session and open a standalone Ptyxis viewer
for it. OC Deck remains visible and animates sessions
reported as busy. Closing the viewer detaches without stopping OpenCode;
quitting OpenCode ends the tmux session and closes the viewer automatically.
Existing OpenCode sessions use their human-readable session name in the Ptyxis
window title; new sessions use the project name until OpenCode assigns a title.
Clicking a project keeps focus in the project list. Press `a` or click **Auto**
in the footer to start a new session in that directory with `--auto`, even if
that project already has running sessions. This does not resume an old session.
Press `Enter` to move into the project's existing sessions; Auto there retains
its session-resume behavior. OpenCode's `--auto` flag auto-approves permission
prompts that are not explicitly denied. Pressing `t` starts a plain shell in
the selected project without launching OpenCode.

On V2, OC Deck first creates a session with `v2.session.create`, then launches
`opencode2` with that explicit session ID. V2 tmux sessions use the `oc2-`
prefix so a migrated ID cannot attach to or stop a V1 `oc-` terminal. Resume
and auto-resume also pass explicit session IDs. Creation sends the required
location in the JSON body. If terminal creation fails before OpenCode starts,
OC Deck removes the otherwise unused session through `v2.session.remove` and
reports any rollback failure explicitly.

Clicking a highlighted session's name opens an inline rename editor. `Enter`
saves through the selected backend, `Esc` cancels, and an empty name cancels
too. V2 uses `v2.session.rename`; the V1 rollback uses its legacy loopback
route. When the selected API is unavailable, the old name is restored and a
notice explains why. Privacy mode blocks renaming so hidden titles stay hidden.

Pending `QUESTION` and `PERMISSION` sessions appear in the attention strip above
the views. Agent STATE labels include elapsed time in the current state, and
`y` approves a selected permission once. V2 reads permission requests with
`v2.permission.request.list`, forms with `v2.form.request.list`, and replies to
permissions with `v2.session.permission.reply`. Forms are shown as QUESTION and
still open in the terminal; OC Deck does not guess answers for typed or
multi-field forms. The V1 rollback retains its legacy permission/question
routes. Every resource in a V2 permission request is retained and shown before
the exact session/request pair can be approved.

If a session already has a live OpenCode TUI process, `o`, `a`, `Enter`, and
clicking the `INST` count attach to that existing terminal instead of starting
a second instance. When the terminal lives in a tmux session, OC Deck finds it
by matching the process's pane tty. On GNOME Wayland, the OC Deck Switch
extension identifies the exact standalone Ptyxis viewer from its tmux process
and raises that window directly. Until the extension reloads, OC Deck uses the
standalone Ptyxis process's unique D-Bus connection for the same exact-window
activation. OC Deck opens another viewer only when it confirms that no viewer
exists. The detail pane names the tmux session each live
terminal runs in. Pressing `x` stops that tmux session — the OpenCode process
ends, the stored session stays resumable — with a double-press confirmation.

In the AGENTS view, `z` or **PURGE AGENT TABS → BACKGROUND** closes every
attached OC Deck terminal window and leaves each tmux session running detached
in the background, so agents keep working and can be reattached later with
`o` or `Enter`. It asks for a second `z` before acting and never stops a tmux
session. Windows that do not run through tmux — direct TUIs and the OC Deck
viewer itself — are left untouched.

Every project owns a deterministic accent color. Project and session rows and
the detail pane use it, and each new tmux session is themed with it — status
bar, pane borders, and a status-left label naming the project — so every
terminal for a project carries the same theme.

`m` minimizes the window (ydotool's Super+H on Wayland, xdotool on X11)
without exiting; OC Deck keeps refreshing in the background.

If a project directory does not exist, OC Deck creates it when the location is
writable. When it cannot (for example a catalog entry on an unmounted drive),
sessions and terminals start in `~/ocdeck-workspaces/<project>` instead and a
notice explains the substitution.

## Portfolio next steps

Press `5` for a terminal-native portfolio briefing. The selected project view
shows its assessment, summary, confidence and evidence age, blockers, completed
outputs, research status, and a vertical `now` / `next` / `blocked` / `done`
step diagram. Confidence is reported as `low`, `medium`, or `high`; evidence age
is `unknown` while queued, running, failed, or otherwise unknown research has no
evidence timestamp. Queued or running research animates on the dashboard's
existing activity clock, including when no OpenCode session is live.

The report header distinguishes running, completed, partial, and failed
artifacts. Reports or evidence older than 24 hours are marked stale. A missing
artifact and a report with no exact match for the selected project have distinct
empty states. `partial` means project research has mixed completed and failed
outcomes; it does not imply normal project omission.

The view is advisory and read-only. It has no command for executing a
recommendation, and process/session actions are blocked whenever the NEXT tab
is active, regardless of keyboard focus or mouse activation.
Report values are rendered as sanitized plain `Rich Text`, never as Rich markup
or Markdown. Privacy mode immediately replaces project and report details with
a hidden-content notice.

## Terminal instance counts

The `INST` session column counts running OpenCode TUI processes for the selected
backend that explicitly
name that session with `--session` or `-s`. If the same session is open in two
terminals, its count is `2`. Plain `opencode` and `--continue` launches do not
expose their current session ID, so they are included in the total as
**unlinked TUI** instances instead of being guessed onto a session. V2 scans
`opencode2`/`opencode2.exe` TUI commands but excludes API, service, and
`serve --service` processes; V1 scans only `opencode`.

Sessions reported as `busy` or `retry` animate their state icon. In V1 rollback
mode, a session whose latest assistant turn is still open also animates from
the read-only metadata fallback (see the agents board below). A live terminal
with no active signal shows a static IDLE marker; only genuine activity
animates.

## Live agents board

Press `4` for the agents board: every session that is currently alive, sorted
by the time of your latest prompt (newest first), with full keyboard navigation
(`j`/`k` to move, `Enter` or `o` to attach). The `AGE` column continues to show
the session's latest update age. Live subagents are grouped beneath their live
parent. Press `Left` on a parent row to expand or collapse its inline list;
pressing `Left` on a leaf subagent collapses the list and returns to its parent.
Each child keeps its own state, terminal exposure, project, age, and detail.
The state cell also shows how long the agent has remained in its current state.

- **PERMISSION (red)** — OpenCode reports a pending permission request
- **QUESTION (purple)** — a tool question is waiting for your answer
- **RUNNING (green)** — the selected backend reports the session active; V1 can
  also use its local plugin and recent unfinished-turn fallback
- **STALLED (yellow)** — in V1 rollback mode, a live TUI's unfinished assistant
  turn has produced no database activity for 15 minutes; inspect or fix the
  terminal before continuing
- **RETRY (amber)** — the session is retrying after an error
- **REVIEW (orange)** — a live terminal whose latest assistant turn completed
  recently, after your latest prompt, and now waits for your judgement
- **IDLE (cyan)** — a live terminal with no active or freshly finished turn
- **CLOSED (slate)** — a previously open agent session with no live terminal

OC Deck remembers the agent sessions that were open, in most-recent-first order,
under `$XDG_STATE_HOME/ocdeck/` (at most 20). The default V1 backend retains the
existing `recently-open-sessions.json`; V2 and explicit remote servers have
separate files. Multiple viewers merge their observations under a file lock,
and a partial/failed session listing never erases remembered IDs.

Closed ones remain listed at the bottom of the agents board. The **REOPEN** bar
shows how many are closed and how many are remembered in total. Select a CLOSED
row and press `o` or `Enter`, or use **REOPEN / Shift+L** twice to reopen the
closed set. Confirmation is tied to the exact IDs and expires after six seconds;
already active or currently launching sessions are skipped. Browser-enabled
sessions reconnect to their server rather than launching a standalone copy.
The same session IDs, directories and backend/server affinity are preserved.
Mobile mode reopens one selected session at a time with `Enter`.

States stay truthful in both directions: an explicitly busy task reads RUNNING;
in V1, a fresh unfinished turn does too and becomes STALLED only after its
fallback activity goes quiet. A stopped job leaves RUNNING, STALLED, or REVIEW
within roughly two seconds whether it is killed from OC Deck's `x` action,
inside the TUI, or by process death elsewhere. A lightweight V1 pulse re-checks
process liveness, tmux mapping, active and pending API state, permission files,
and read-only session database metadata. The V2 pulse additionally re-lists
sessions, projects, routes, the Markdown catalog, durable registry, and named
agents so service epochs and external catalog changes reconcile promptly. If a
V2 active-state poll fails, OC Deck retains the last known state and marks the
connection `DEGRADED` or `OFFLINE` with an explicit stale-state detail. It does
not infer that running sessions became idle from a failed response. A valid
empty active-state response clears the old state. The slower full collection sweep keeps its regular
cadence for services, machine metrics, and portfolio briefings.
A dead terminal never stays RUNNING merely because its last message row is
missing a completion timestamp.

Full refreshes and activity pulses are generation-ordered: an older, slower
pulse cannot overwrite a newer full snapshot and roll labels back to stale
values.
Rendering errors are reported as DEGRADED in both the header and Signal card;
a successful refresh restores the live indicator. Repeated identical errors
produce one notice rather than a toast on every pulse.

### State architecture

The terminal UI stays on Python/Textual. `runtime_state.py` contains typed,
side-effect-free reconciliation rules; `source.py` validates process identity
and backend boundaries before passing snapshots to that layer. A newer,
complete snapshot from a resumed session owner can supersede an older
instance's abandoned prompt. Missing or malformed request lists are not treated
as replies, and newer requests remain visible.

`recent_open.py` owns persistence and open/closed observations independently of
display filtering. The CLI injects backend-scoped storage into the UI; embedded
or test instances use in-memory history unless storage is explicitly supplied.
Normal Open and bulk restoration share one session-routing function.

The `TERM` column distinguishes terminal exposure:

- **OPEN** — the tmux session currently has an attached terminal viewer
- **BG TMUX** — OpenCode is running in tmux without an attached viewer
- **DIRECT** — the live OpenCode TUI is not mapped to a tmux session

V2 pending permission and form state comes from the authenticated OpenAPI
operations. V1 can additionally reconcile state published locally by the
permission-notify plugin, so rollback-mode requests can remain visible when its
legacy API is locked.

## Token usage and what is left

Press **7** for the **USAGE** view: one block per provider, Anthropic (Claude Code)
first, then OpenAI (Codex) and every provider reached through OpenCode. It reads
local files only (no network, no credentials) and re-reads every 30 seconds while
it is showing (`r` refreshes now).

| Source | Tokens spent (5h / 24h / 7d) | What is left |
| --- | --- | --- |
| Claude Code | summed from `~/.claude/projects` transcripts, each message once (helpers included) | the plan's real 5h and 7d usage and reset time, reported by Claude Code to a status line that OC Deck attaches to the sessions it launches (`usage/claude-rate-limits.json`) |
| Codex | summed from the `token_count` events in `~/.codex/sessions` | the rate-limit reading stored in every rollout; one whose window has reset is shown as "waiting for a new reading", never as a stale percentage |
| OpenCode providers | per `providerID` from the V2 database (read-only), with cost where OpenCode records one | nothing is reported, so set a budget (below) |

"Tokens" are fresh input plus output; cache reads are listed apart as "cached"
because they are most of the count and cost far less. A Claude reading appears
once a session launched from OC Deck has answered; sessions started elsewhere do
not report it.

To see "left" for a provider that reports no limit, add a budget in
`~/.config/ocdeck/usage.json`. Keys are `claude-code`, `codex` or
`opencode:<providerID>` (the tab prints the ones it found); windows are `5h`,
`24h` or `7d`; units are `usd` and/or `tokens`:

```json
{"limits": {"opencode:opencode-go": {"7d": {"usd": 30}}, "opencode:deepseek": {"24h": {"tokens": 20000000}}}}
```

A real provider limit is never replaced by a budget. `python -m ocdeck.usage`
prints the same figures as text.

## Sentinel health and alarms

Press **6** for the read-only **ALARMS** view. It reads the bounded artifact
at `$XDG_STATE_HOME/ocdeck/sentinel-alarms.json` (normally
`~/.local/state/ocdeck/sentinel-alarms.json`). Records are newest first and
include **Unassigned** projects; project/session filters never hide global
alarms. Highlight a record for its summary, identity and evidence outcome.
Enter locates a session only when its harness, native ID and directory match
exactly. Process launches, approvals and browser grants are blocked in this
view. `p` hides alarm identities and details while retaining safe counts.

A persistent strip and `ocdeck --once` report the Sentinel health state:

- **OFFLINE**: no artifact, or the last completed scan is at least 60 seconds old.
- **UNAVAILABLE**: unreadable/invalid artifact, a broken chain, or a future scan timestamp.
- **DEGRADED**: reported coverage, rules, prior-integrity or overflow gaps.
- **OBSERVED**: a fresh scan with no reported gaps.

The TUI checks health every five seconds independently of session collection,
so a stalled backend cannot hide scanner failure. Old valid records remain
visible when scans stop; rejected records are cleared. The strip includes
unresolved critical and omitted-record counts. Health and records come from
one validated read. The observation pilot is not authenticated provenance or
proof of enforcement; the current scanner reports unsupported S10/S11 surfaces.

Run one scan with:

```bash
.venv/bin/python -B -m ocdeck.sentinel --once
```

Optional user-unit templates in `systemd/ocdeck-sentinel.{service,timer}` run
one scan at a time, then schedule the next scan 15 seconds after completion.
Enable them explicitly from your checkout (the unit templates contain
`@OCDECK_ROOT@`, which `sed` fills in):

```bash
mkdir -p ~/.config/systemd/user
sed "s|@OCDECK_ROOT@|$PWD|g" systemd/ocdeck-sentinel.service > ~/.config/systemd/user/ocdeck-sentinel.service
install -Dm644 systemd/ocdeck-sentinel.timer ~/.config/systemd/user/ocdeck-sentinel.timer
systemctl --user daemon-reload
systemctl --user enable --now ocdeck-sentinel.timer
```

The scanner reads `~/.config/ocdeck/sentinel-rules.json`, retains unresolved
alarm records and sends deduplicated notifications. Missing rules produce S8
and a degraded report. Stop scheduled scans with
`systemctl --user disable --now ocdeck-sentinel.timer`; the dashboard will
show OFFLINE after the last artifact becomes stale.

## Data sources

The default V2 backend uses these OpenAPI operation IDs through `opencode2 api`:

- `v2.health.get`, `v2.session.list`, and `v2.session.active`
- `v2.project.list`, `v2.agent.list`, and `v2.mcp.list`
- `v2.debug.location.list` for authoritative loaded-location discovery
- `v2.permission.request.list` and `v2.form.request.list`
- `v2.session.create`, `v2.session.rename`, and
  `v2.session.permission.reply` for explicit user actions; failed unused
  launches may use `v2.session.remove` for rollback

`v2.session.list` follows opaque `cursor.next` values and normalizes
`Session.Info` location, parent, agent, model, timestamps, and Home Agent
metadata into OC Deck's bounded in-memory model. Location-scoped calls always
send `location[directory]` and, when present, `location[workspace]`; their
returned location envelopes are validated. V2 never opens OpenCode's
SQLite database, reads the shared-service credential file, or falls back to a
V1 CLI/API when a V2 request fails. It also does not request message bodies,
tool output, transcripts, or logs.

The explicit V1 rollback backend retains these legacy sources:

- `opencode session list --format json --pure`
- `opencode debug scrap --pure`
- OpenCode's session database, opened read-only, solely for session metadata:
  archived IDs (`session.time_archived`), native parent IDs, your latest prompt
  time per session, and each session's latest assistant-turn timestamps and
  finish reason (`message.data` JSON fields only). Only native parent IDs — a
  subagent actually spawned by its parent agent — group live subagents without
  changing project routing; worker sessions launched by an orchestrator stay
  top-level. Archived sessions never
  appear in OC Deck's session list, project panes, counts, or agents board; turn
  metadata drives the truthful RUNNING / REVIEW / OPEN states. Tool output is
  never queried
- OpenCode V1's loopback HTTP health/status endpoints (including pending
  permission requests) when `OPENCODE_SERVER_PASSWORD` is available

Both backends use these bounded local sources:

- The configured Markdown project catalog and exact session-route map
- The configured, size-bounded Home Agent `latest.json` briefing artifact
- `systemctl --user` for a small allowlist of local services
- Same-user `/proc/<pid>/comm` and `/proc/<pid>/cmdline` for OpenCode TUI counts
- `/proc/<pid>/fd/0` and `tmux list-panes` to locate the tmux session behind a
  live terminal
- `/proc` aggregate files and `shutil.disk_usage` for machine health

V1 session metadata collection uses bounded parallelism across catalog project
directories and retries one transient CLI failure before displaying a warning.

OC Deck never reads `auth.json`, OpenCode transcripts, tool output, or logs.
The V1 rollback opens SQLite read-only for the metadata described above and may
read the explicitly configured V1 `server.env`; it writes neither file. The V2
backend reads neither. OC Deck does not read process environments. It keeps
snapshots in memory only. Background collection does not call `home-agentctl`
or inspect Home Agent transcripts or state files; the briefing artifact is its
only Home Agent input. Registering a project invokes `home-agentctl` only after
the explicit `d` action. If the database is missing, locked, or
unreadable in V1 rollback mode, OC Deck shows every session as usual instead of
failing, with live terminals falling back to OPEN rather than guessing RUNNING
or REVIEW.
