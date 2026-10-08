#!/usr/bin/env python3
"""Real-terminal shortcut check for OC Deck.

Runs OC Deck inside a *private* tmux server, presses real keys, and reads the
rendered screen. Everything is sandboxed: temporary HOME/XDG dirs, fake
``claude``/``codex`` binaries that only log their arguments, no display or
session D-Bus (so no windows open and no GNOME calls are made), and a tmux
socket that is not the user's.

Usage: .venv/bin/python tests/e2e_keys.py [--keep]
Exit status 0 when every check passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OCDECK = ROOT / ".venv/bin/ocdeck"
SESSION = "e2e-deck"
CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
CODEX_ID = "99999999-8888-7777-6666-555555555555"
HARNESSES = {
    # harness: (native id, tmux prefix, RUNTIME text in the detail pane, resume argv tail, search words)
    "claude": (CLAUDE_ID, "cc", "Claude Code · claude-opus-5-5", ["--resume", CLAUDE_ID], "Parser"),
    "codex": (CODEX_ID, "cx", "Codex · gpt-6-codex", ["resume", CODEX_ID], "lexer"),
}


class Sandbox:
    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="ocdeck-e2e-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None))
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.log = self.base / "calls.jsonl"
        self.tmux_dir = self.base / "tmux"
        self.socket = self.tmux_dir / "server.sock"
        for path in (self.home, self.bin, self.tmux_dir):
            path.mkdir(parents=True)
        self.tmux_dir.chmod(0o700)
        self.project = self.base / "work" / "alpha"
        self.project.mkdir(parents=True)
        self._fake_cli("claude")
        self._fake_cli("codex")
        self._claude_transcript()
        self._codex_transcript()
        # A stand-in agent-browser install so Shift+B can grant (never started).
        launcher = self.home / ".local/lib/opencode/playwright-mcp-v1"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        (self.home / ".config/opencode").mkdir(parents=True)
        (self.home / ".config/opencode/agent-browser.json").write_text("{}")
        agents = self.project / ".claude/agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: Fixture reviewer\n---\n")
        (self.home / ".codex/review.config.toml").write_text('model = "fixture-model"\n')
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TMUX_TMPDIR": str(self.tmux_dir),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_RUNTIME_DIR": str(self.base / "run"),
            "OCDECK_HUB_DIR": str(self.home / ".config/agents"),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        }
        (self.base / "run").mkdir(mode=0o700)

    def _fake_cli(self, name: str) -> None:
        script = self.bin / name
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            f"open({str(self.log)!r}, 'a').write(json.dumps({{'cli': {name!r}, 'argv': sys.argv[1:], "
            "'cwd': os.getcwd()}) + '\\n')\n"
            "print('fake', sys.argv); time.sleep(600)\n"
        )
        script.chmod(0o755)

    def _claude_transcript(self) -> None:
        folder = self.home / ".claude/projects" / str(self.project).replace("/", "-")
        folder.mkdir(parents=True)
        entries = [
            {"type": "user", "cwd": str(self.project), "timestamp": "2026-09-26T10:00:00Z",
             "message": {"content": "refactor the parser"}},
            {"type": "assistant", "cwd": str(self.project), "timestamp": "2026-09-26T10:01:00Z",
             "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]}},
            {"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-26T10:01:01Z"},
            {"type": "custom-title", "customTitle": "Parser refactor"},
        ]
        (folder / f"{CLAUDE_ID}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

    def _codex_transcript(self) -> None:
        folder = self.home / ".codex/sessions/2026/09/26"
        folder.mkdir(parents=True)
        entries = [
            {"type": "session_meta", "timestamp": "2026-09-26T09:00:00Z",
             "payload": {"id": CODEX_ID, "cwd": str(self.project), "timestamp": "2026-09-26T09:00:00Z"}},
            {"type": "turn_context", "timestamp": "2026-09-26T09:00:01Z",
             "payload": {"cwd": str(self.project), "model": "gpt-6-codex"}},
            {"type": "event_msg", "timestamp": "2026-09-26T09:00:02Z",
             "payload": {"type": "user_message", "message": "port the lexer to rust"}},
            {"type": "event_msg", "timestamp": "2026-09-26T09:05:00Z", "payload": {"type": "task_complete"}},
        ]
        name = f"rollout-2026-09-26T09-00-00-{CODEX_ID}.jsonl"
        (folder / name).write_text("".join(json.dumps(e) + "\n" for e in entries))

    def tmux(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        # Pin every operation, including cleanup, to our own socket. Never
        # inherit TMUX, display/DBus addresses, auth variables or user tmux config.
        return subprocess.run(["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
                              env=self.env, capture_output=True,
                              text=True, timeout=10, check=check)

    def start(self) -> None:
        self.tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50",
                  "-c", str(self.project), str(OCDECK), "--harness", "claude,codex", "--refresh", "2", check=True)
        self.wait_for("LIVE", 30)

    def screen(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", SESSION).stdout

    def keys(self, *keys: str, pause: float = 0.6) -> None:
        for key in keys:
            self.tmux("send-keys", "-t", SESSION, key)
            time.sleep(pause)

    def wait_for(self, text: str, seconds: float = 8) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if text in self.screen():
                return True
            time.sleep(0.25)
        return False

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def sessions(self) -> set[str]:
        out = self.tmux("list-sessions", "-F", "#{session_name}").stdout
        return set(out.split())

    def close(self, keep: bool) -> None:
        self.tmux("kill-server")  # Explicit -S pins this to the disposable socket.
        if not keep:
            shutil.rmtree(self.base, ignore_errors=True)


def run(keep: bool) -> int:
    box = Sandbox()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    try:
        box.start()
        check("starts and shows LIVE", "LIVE" in box.screen())
        for key, label in (("1", "OPERATIONS"), ("2", "SERVICES"), ("3", "KEYS"), ("4", "AGENTS"), ("5", "NEXT"), ("6", "ALARMS")):
            box.keys(key)
            check(f"key {key} opens {label}", label in box.screen())
        box.keys("1")
        check("missing Sentinel scan is visibly OFFLINE", "SENTINEL-OFFLINE" in box.screen())
        listed = box.screen()
        check("Claude session listed with CC badge", "CC Parser refactor" in listed)
        check("Codex session listed with CX badge", "CX port the lexer" in listed)
        box.keys("r", pause=1.0)
        check("r refreshes and stays LIVE", "LIVE" in box.screen())

        words = {entry[2]: entry[4] for entry in HARNESSES.values()}

        def select(runtime: str) -> bool:
            """Search for the session, then move into the results (the real user path)."""
            box.keys("1", "/", pause=0.4)
            box.keys("C-u", pause=0.2)  # empty the search field
            box.tmux("send-keys", "-t", SESSION, "-l", words[runtime])
            time.sleep(0.5)
            box.keys("Down", pause=0.5)
            return runtime in box.screen()

        for harness, (native, prefix, runtime, resume_tail, _words) in HARNESSES.items():
            key = f"{harness}:{native}"
            tag = harness.capitalize()
            check(f"{tag}: cursor reaches the session", select(runtime), box.screen()[-400:])
            box.keys("y", pause=0.8)
            expected = "No pending permission" if harness == "claude" else "OpenCode-only"
            check(f"{tag}: y says {expected!r}", expected in box.screen())
            before = len(box.calls())
            box.keys("o", pause=1.5)
            new = box.calls()[before:]
            check(f"{tag}: o resumes it", any(c["cli"] == harness and c["argv"][-len(resume_tail):] == resume_tail
                                              for c in new), str(new))
            check(f"{tag}: runs in managed {prefix}- tmux", f"{prefix}-{native}" in box.sessions(), str(box.sessions()))
            header = box.tmux("show-options", "-t", f"={prefix}-{native}:").stdout
            check(f"{tag}: uniform top header identifies the harness",
                  "status-position top" in header and "@ocdeck_harness" in header
                  and ("Claude Code" if harness == "claude" else "Codex") in header, header)
            select(runtime)
            box.keys("o", pause=1.5)
            check(f"{tag}: o on the live session starts no second process", len(box.calls()) == before + 1)
            box.keys("4", pause=1.0)
            check(f"{tag}: live on AGENTS with RUNTIME", runtime in box.screen())
            box.tmux("resize-window", "-t", f"={SESSION}:", "-x", "60", "-y", "50")
            time.sleep(0.5)
            code = "CC OP5" if harness == "claude" else "CX CDX"
            check(f"{tag}: RUNTIME stays visible at 60 columns", code in box.screen(), box.screen())
            box.tmux("resize-window", "-t", f"={SESSION}:", "-x", "200", "-y", "50")
            select(runtime)
            box.keys("B", pause=1.0)
            grants = box.home / ".config/ocdeck/browser-grants.json"
            check(f"{tag}: Shift+B grants the agent browser", grants.exists() and key in grants.read_text())
            box.keys("B", pause=1.0)
            check(f"{tag}: Shift+B again revokes it", key not in grants.read_text())
            select(runtime)
            box.keys("x", "x", pause=1.5)
            check(f"{tag}: x x stops its tmux job", f"{prefix}-{native}" not in box.sessions(), str(box.sessions()))

        box.keys("4", pause=0.8)
        before = len(box.calls())
        box.keys("L", "L", pause=1.5)
        relaunched = {c["cli"] for c in box.calls()[before:]}
        check("Shift+L L reopens both previous sessions", relaunched == {"claude", "codex"}, str(relaunched))
        for harness, (native, prefix, runtime, _, _words) in HARNESSES.items():
            select(runtime)
            box.keys("x", "x", pause=1.2)

        # New sessions in each harness (Shift+H picks the harness).
        for wanted, argv_check in (("Claude Code", lambda a: "--session-id" in a), ("Codex", lambda a: "resume" not in a)):
            for _ in range(3):
                box.keys("H", pause=0.6)
                if f"launch with {wanted}" in box.screen():
                    break
            check(f"Shift+H selects {wanted}", f"launch with {wanted}" in box.screen())
            box.keys("1", pause=0.4)
            before = len(box.calls())
            box.keys("n", pause=1.5)
            new = box.calls()[before:]
            cli = "claude" if wanted == "Claude Code" else "codex"
            check(f"n starts a new {wanted} session", any(c["cli"] == cli and argv_check(c["argv"])
                  and c["cwd"] == str(box.project) for c in new), str(new))

        # Launch picker: real keyboard navigation for both native choices.
        select(HARNESSES["claude"][2])
        before = len(box.calls())
        box.keys("S", pause=0.8)
        check("Shift+S opens the project launch picker", "Choose harness and agent" in box.screen())
        box.keys("Escape", pause=0.5)
        check("Escape cancels the picker without launching", len(box.calls()) == before)
        for cli, position, flag, agent in (("claude", "Home", "--agent", "reviewer"),
                                          ("codex", "End", "--profile", "review")):
            select(HARNESSES["claude"][2])
            before = len(box.calls())
            box.keys("S", "Enter", position, "Enter", "Tab", "Enter", "Down", "Enter", "Tab", "Enter", pause=0.35)
            time.sleep(1)
            new = [call for call in box.calls()[before:] if call["cli"] == cli]
            check(f"Shift+S selects {cli} {agent} in the same project",
                  any(call["argv"][:2] == [flag, agent] and call["cwd"] == str(box.project) for call in new), str(new))

        # Handoff Claude -> Codex (Shift+H is on Codex now).
        select(HARNESSES["claude"][2])
        before = len(box.calls())
        box.keys("C", pause=2.5)
        handoff = [c for c in box.calls()[before:] if c["cli"] == "codex"]
        notes = list((box.home / ".config/agents/handoffs").rglob("*.md"))
        check("Shift+C hands the Claude session to Codex", bool(handoff) and bool(notes), str(handoff)[:200])
        check("handoff prompt points at the notes", bool(handoff) and bool(notes) and str(notes[0]) in " ".join(handoff[0]["argv"]))

        box.keys("t", pause=1.2)
        check("t opens a project shell terminal", any(name.startswith("oc-sh-") for name in box.sessions()))
        box.keys("4", pause=0.5)
        box.keys("z", "z", pause=1.2)
        check("z z runs without touching tmux jobs", any(n.startswith("oc-sh-") for n in box.sessions()))
        box.keys("f", pause=0.6)
        check("f toggles the project scope", "Session list:" in box.screen())
        box.keys("f", pause=0.6)
        box.keys("p", pause=0.6)
        private = box.screen()
        check("p hides names, prompts and RUNTIME",
              all(text not in private for text in ("Parser refactor", "port the lexer", "claude-opus", "gpt-6-codex")))
        box.keys("p", pause=0.6)

        box.keys("1", "/", pause=0.4)
        before = len(box.calls())
        box.keys("H", "S", "C", "o", "x", "q", pause=0.3)  # typed into search: must not act
        check("letters typed in search trigger no actions", len(box.calls()) == before and "LIVE" in box.screen())
        box.keys("Escape", "Escape")  # first clears the text, second leaves the search box

        box.keys("q", pause=1.5)
        check("q quits", SESSION not in box.sessions() or "LIVE" not in box.screen())
    except Exception as error:  # report, never hide
        check("harness error", False, f"{type(error).__name__}: {error}")
    finally:
        box.close(keep)

    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"{mark}  {name.ljust(width)}  {'' if ok else detail}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f" (sandbox kept: {box.base})" if keep else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run("--keep" in sys.argv[1:]))
