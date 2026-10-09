# Dedicated agent browser

OpenCode agents share a separate persistent Chrome instance. It runs
**headed** so sign-in and verification prompts are visible on the monitor named
in its settings (`monitor`, e.g. `eDP-1`). The normal Chrome profile is never touched.
Headless remains an explicit option for sites that support unattended browsing;
it is not the reliable default for the signed-in ChatGPT workflow.

## Install

Needs Google Chrome (or Chromium), Python 3.12+, and a graphical session. From the
repository root:

```bash
cd agent-browser
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# a command to drive it
printf '#!/bin/sh\nexec "%s/.venv/bin/python" -B "%s/agent_browser.py" "$@"\n' "$PWD" "$PWD" \
    > ~/.local/bin/oc_agent_browser
chmod +x ~/.local/bin/oc_agent_browser

# the service (starts on demand)
mkdir -p ~/.config/systemd/user
sed "s|@OCDECK_ROOT@|$(dirname "$PWD")|g" opencode-agent-browser.service \
    > ~/.config/systemd/user/opencode-agent-browser.service
systemctl --user daemon-reload
```

Then create `~/.config/opencode/agent-browser.json`. Change the paths and monitor to
yours; `eDP-1` is just an example connector:

```json
{
  "enabled": true,
  "headless": false,
  "monitor": "eDP-1",
  "port": 9223,
  "profile": "/home/you/.local/share/opencode/agent-browser-profile",
  "browser": "/usr/bin/google-chrome-stable",
  "start_url": "https://chatgpt.com/"
}
```

Run `oc_agent_browser` to start it, sign in to your sites once in that window, and
point your agents' browser tool (Playwright MCP, for example) at
`http://127.0.0.1:9223`.

- Profile: `~/.local/share/opencode/agent-browser-profile`
- Settings: `~/.config/opencode/agent-browser.json`
- Service: `opencode-agent-browser.service` (started on demand by MCP)
- Local CDP endpoint: `http://127.0.0.1:9223`
- Both `browser_*` and the compatible `signed_in_tabs_*` tools connect to it.

Sign in to ChatGPT, Claude, Google Drive, or other sites once in this browser.
The profile retains those logins across browser and OpenCode restarts. Browser
automation is via Playwright/CDP rather than the desktop mouse. Agents can use
separate tabs in this browser while you use your regular browser.

```bash
oc_agent_browser                         # start it (first start places it on the configured monitor) and show status
oc_agent_browser status                  # profile, endpoint, and actual window bounds
oc_agent_browser doctor                  # read-only connection/page health; exit 2 for a recognized block
oc_agent_browser recover                 # use the same profile in headed mode; no task/message replay
oc_agent_browser clean                   # close agent tabs, keeping the current one
oc_agent_browser mode headless           # restart with no window/GPU (lower memory, for unattended jobs)
oc_agent_browser mode headed             # restart with a visible window
```

### Tab cleanup and memory

While the browser runs, a small janitor keeps it tidy. All of it is configurable in
`agent-browser.json`:

| Setting | Default | Meaning |
|---|---|---|
| `purge_idle_minutes` | `15` | Close an ordinary tab whose address and title have not changed for this long. `0` turns the janitor, including the memory guard, off. |
| `max_tabs` | `12` | Above this many tabs, close the oldest idle ordinary tabs first (a tab must have been idle for 2 minutes). `0` removes the cap. |
| `keep_tabs` | Google Calendar | Address prefixes that are never closed. |
| `memory_guard` | `true` | Watch the memory of agent chat tabs (below). |
| `flag_dir` | `agent-browser/flags` | Where the memory guard leaves its notes. |
| `browser_lock` | none | A lock file your agents take for every browser action (`flock`); the guard only reloads a tab when it can take it without waiting. |

**Agent chat tabs are never closed.** Each agent opens its tab with a marker in the
address, `?agentA=1`, `?agentB=1`, `?agentC=1`, and so on. The janitor leaves any tab
carrying `?agentX=1` alone, because an agent may be waiting on a long conversation in it.

**Heavy chat tabs are measured, not closed.** A very long chat makes its tab grow. Every
2 minutes the guard reads each agent tab's JavaScript heap over a throwaway DevTools
session. At **1.5 GB** it writes `flags/agentX_reload` (one line) so the owning agent can
reload its own tab; the flag is removed once the tab is small again. At **3 GB**, or for
the heaviest flagged tab while the machine has under **2 GB** of free memory, the guard
reloads the tab itself, only if `browser_lock` is free and at most once every 10 minutes.
The chat stays on the server, so a reload loses nothing but an unsent draft. It never
closes a tab. The thresholds are constants at the top of `agent_browser.py`.

### Keeping it light

The agent profile starts as a copy of the owner's, extensions included, and most memory
goes to the claude.ai chat pages themselves. Two things trim the rest:

- `"extensions": false` in `agent-browser.json` launches Chrome with `--disable-extensions`: no
  background page per extension and no extension scripts injected into every chat. Agents drive
  the browser over the debugging port, so none is needed; sign-ins live in the profile and are
  unaffected. The old OpenCode V1 tools attach through the Playwright *extension*, so set it back
  to `true` if you roll back to V1. It takes effect at the next browser start.
- When Chrome reopens the saved tabs it drops claude.ai's `artifact=` panel parameter (each open
  panel is another renderer) and repeated addresses (a second copy of a long chat). The saved
  file itself is left alone.

### Cloudflare / sign-in recovery

In testing, ChatGPT remained on a Cloudflare 403 verification page
in headless mode. Switching the same persistent profile to headed mode loaded
the signed-in account normally. The headed setting is saved across restarts.
This fixes the observed failure mode, not every possible third-party challenge.

1. Run `oc_agent_browser doctor`. A reachable CDP endpoint means the browser is
   connected, not that the site is authenticated or the target conversation loaded.
   Known challenge/block titles are reported per tab; other pages are explicitly
   `unverified` and must be inspected with the browser snapshot tools.
2. Use `oc_agent_browser recover` when the shared browser is idle. It keeps the
   same profile and uses headed mode on the saved monitor. An already
   headed, connected browser is left running. Mode changes restart Chrome, so
   save any unsent draft before switching. Open web destinations are checkpointed
   before mode changes and normal service stops, then reopened on startup (with
   native Chrome session restoration as a fallback). Draft text is not captured;
   recovery never types or submits a message.
3. Inspect the exact original conversation. If a human-verification/sign-in
   prompt remains, ask the user to complete it in that visible window. Do not
   repeatedly refresh, spoof browser identity, reset cookies, or create another
   profile to work around it.
4. Keep the prepared message and exact destination in the project's durable
   notes. After access returns, inspect the latest conversation to distinguish
   unsent work from an uncertain submission, then resume once. Never replay an
   uncertain submission solely because the browser reconnected.

`doctor` is read-only. No health check automatically restarts the shared browser,
clicks a challenge, switches profiles, or sends a pending project message.

In headed mode it opens on the configured monitor when Chrome starts.
After that, drag the window anywhere you like — OpenCode agents only drive tabs
and never move the window; placement is skipped entirely when the window is
already at its target, so it is not re-presented or raised. The optional
`oc_agent_browser place --monitor <connector>` command can move and save a
different startup connector for scripting, but manual dragging needs no setting.

Chrome runs through XWayland so monitor coordinates are honored under GNOME
Wayland. At startup the service discovers the configured connector's current
geometry and places the dedicated browser within it. Each MCP process connects
to the same live browser, avoiding multiple processes fighting over one profile.

### Agent playbook for SPAs (ChatGPT, Claude, etc.)

Element refs (`f1e1144…`) are generated per snapshot and die on the next render.
Never reuse a ref or a `find` result across calls; act on refs from the most
recent call. A few habits remove almost all of the difficulty seen in practice:

- **Uploads:** use `browser_drop` with absolute paths on the chat composer, or
  call `browser_file_upload` immediately after the click that opens the file
  chooser. Clicking through the menu into the OS file dialog can never finish.
- **Model pickers:** don't automate them. They are custom menus with shifting
  inner targets. Keep one chat with a fixed model; change it by hand if needed.
- **Sending:** fill the whole message once with `browser_type`. In ChatGPT, use
  the enabled **Send prompt** button from a fresh snapshot, then verify the new
  user-message bubble and response. Enter/`submit=true` can leave the text in the
  rich-text composer; if it is confirmed unsent, click Send once rather than
  waiting for a nonexistent response. Never retype or replay an uncertain send.
- **Shared tabs:** call `browser_tabs` (`action: "list"`) first; when several
  agents share this browser, open or select your own tab before acting.
- **Waits:** `browser_wait_for` beats repeat snapshots while a page streams.

The service is capped at `MemoryHigh=2500M`/`MemoryMax=5G` and Chrome runs with
`--renderer-process-limit=6` plus small disk caches so agent tabs cannot drive
the whole machine into swap. `headless` in `agent-browser.json` (or
`oc_agent_browser mode …`) selects the unattended mode; logins persist either
way. The separate `pressure-gc` user timer disposes idle OpenCode instances and
retires idle client sessions when `MemAvailable` or swap enters pressure — see
`../home_agent/deploy/pressure-gc.py`.

`config/playwright-mcp-v1` dispatches to this browser when
`OPENCODE_AGENT_BROWSER_CONFIG` is supplied by the MCP configuration. Without
that setting its legacy extension connection is retained for explicit legacy
use. The dedicated route does not read the extension credential.

After changing MCP configuration, restart the OpenCode backend when its sessions
are idle and reopen standalone clients. Profile logins are independent of that
restart.

Verification:

```bash
.venv/bin/python -B -m unittest test_agent_browser -v
```
