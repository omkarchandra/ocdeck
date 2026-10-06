# Home Agent (optional, bring your own)

OC Deck can show and drive a **project orchestrator** — an agent (or script) of
yours that knows your projects, keeps a catalog of them, writes progress
briefings and starts worker sessions. The author runs one called Home Agent.
**It is not part of this repository**, and OC Deck works fully without it: with
nothing here installed, those features simply stay hidden.

If you want the same, build your own. This page is the contract: everything
OC Deck reads from, or calls on, an orchestrator. All of it is optional, and
each item turns on a feature the moment it exists.

| What you provide | Where OC Deck looks | What it enables |
|---|---|---|
| **Project catalog** — a Markdown table with `Project`, `Host`, `Code` (the folder) and `Vault note` columns | `~/.config/home-agent/projects.md`, or `ocdeck --projects-file PATH` | Named projects in the project list, instead of folders discovered from session history |
| **Session routes** — JSON mapping sessions to catalog projects | `<vault>/Projects/_session-routes.json`, or `--session-routes-file PATH` | Sessions grouped under the right project, helpers following their parent |
| **Briefings** — JSON report, `schemaVersion: 1` (see below) | `$XDG_STATE_HOME/home-agent/reports/latest.json`, or `--briefings-file PATH` | The `NEXT` view: per-project assessment, blockers and next steps |
| **Named agent definitions** — Markdown files, registered in `DEFAULT_NAMED_AGENT_FILES` in `src/ocdeck/source.py` | none by default | The agents board shows your named agents with their state and open sessions |
| **`home-agentctl register-project NAME PATH`** — prints `{"name": ...}` JSON | `$HOME_AGENTCTL` or `~/.local/bin/home-agentctl` | Registering a discovered folder as a managed project from the deck |
| **Browser grant helper** — a Python script (see `src/ocdeck/browser_access.py`) | `OCDECK_BROWSER_GRANT_HELPER=/path/to/script.py` | `Shift+B` / `Shift+N`: sessions that may use the signed-in agent browser |
| **`home-agent-monitor.timer`** — a systemd user timer | systemd user units | A "Home Agent" line in the services panel |

## Briefing report format

```json
{
  "schemaVersion": 1,
  "reportID": "2026-10-04T06:00Z",
  "generatedAt": "2026-10-04T06:00:12Z",
  "status": "completed",
  "projects": [{
    "projectID": "my-project",
    "projectPath": "/home/you/code/my-project",
    "name": "My project",
    "assessment": "on-track",
    "confidence": "medium",
    "researchStatus": "completed",
    "summary": "One or two plain sentences.",
    "evidenceAt": "2026-10-04T05:58:00Z",
    "completedOutputs": [], "blockers": [], "nextSteps": [], "evidence": []
  }]
}
```

`status` is `running`, `completed`, `partial` or `failed`; `assessment` is
`on-track`, `at-risk`, `blocked`, `waiting`, `complete` or `unknown`;
`confidence` is `low`, `medium` or `high`; `researchStatus` is `queued`,
`running`, `completed` or `failed`. The exact limits (sizes, list lengths) are
in `src/ocdeck/source.py` (`parse_briefings`). A report that does not match
is ignored whole rather than half-shown.

## Naming

OC Deck recognises these agent names out of the box: `home_agent`, `maverik`
and `jasmine` (and the alias `jarvis`). They are the author's names; change them
in `src/ocdeck/models.py` (`EXPECTED_HOME_AGENT_ROLES`) to match yours. An
orchestrator that is not configured, loaded or running anywhere is not listed.

## Security

Anything you add here can start agents and widen what they may do. Keep it
outside the agents' reach (root-owned, or at least not writable by the agent
sessions it controls), and treat files OC Deck reads from it as untrusted input
the way OC Deck does: it size-limits and validates every one of them.
