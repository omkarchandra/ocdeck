"""Project-scoped harness/agent selection; discovery never launches a CLI."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from .harnesses import HARNESS_LABELS


@dataclass(frozen=True)
class LaunchChoice:
    harness: str
    agent: str = ""
    handoff: bool = False


def agent_arguments(harness: str, agent: str) -> list[str]:
    if not agent:
        return []
    pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
    if harness in {"opencode", "claude"}:
        pattern = r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}"
    if re.fullmatch(pattern, agent) is None or ".." in agent.split("/"):
        raise ValueError("Use a configured agent/profile name, not command-line options")
    flag = {"opencode": "--agent", "claude": "--agent", "codex": "--profile"}.get(harness)
    if flag is None:
        raise ValueError("This harness supports its default agent only")
    return [flag, agent]


def _frontmatter(path: Path) -> dict[str, str]:
    """Read bounded simple frontmatter fields, never interpret YAML or prompts."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            text = stream.read(8192).decode("utf-8")
    except (OSError, UnicodeError):
        return {}
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result = {}
    for line in lines[1:]:
        if line == "---":
            return result
        match = re.fullmatch(r"(name|description|mode|hidden|disable):\s*(.*?)\s*", line)
        if match:
            result[match[1]] = match[2].split(" #", 1)[0].strip("\"'")
    return {}


def _markdown_agents(root: Path, harness: str) -> set[str]:
    names: set[str] = set()
    # Bounded traversal, no linked directories or linked agent files.
    if root.is_symlink():
        return names
    visited = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        base = Path(directory)
        visited += 1
        if visited > 256:
            break
        folders[:] = sorted(name for name in folders if not name.startswith(".")
                            and not (base / name).is_symlink())[:64]
        if len(base.relative_to(root).parts) >= 4:
            folders[:] = []
        for filename in sorted(files)[:256]:
            if not filename.endswith(".md"):
                continue
            path = base / filename
            fields = _frontmatter(path)
            if not fields:
                continue
            if harness == "claude":
                name = fields.get("name", "") if fields.get("description") else ""
            else:
                if (fields.get("mode") == "subagent" or fields.get("hidden") == "true"
                        or fields.get("disable") == "true"):
                    continue
                name = path.relative_to(root).with_suffix("").as_posix()
            try:
                agent_arguments(harness, name)
            except ValueError:
                continue
            if name:
                names.add(name)
    return names


def discover_agents(project: Path, opencode_names: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    names = {"opencode": {"build", "plan"}, "claude": set(), "codex": set()}
    for name in opencode_names:
        try:
            agent_arguments("opencode", name)
        except ValueError:
            continue
        if name:
            names["opencode"].add(name)
    for folder in ("agents", "agent"):
        for root in (config / "opencode" / folder, project / ".opencode" / folder):
            names["opencode"].update(_markdown_agents(root, "opencode"))
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    for root in (claude_home / "agents", project / ".claude" / "agents"):
        names["claude"].update(_markdown_agents(root, "claude"))
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    # Codex 0.157's --profile selects <name>.config.toml. Read names only.
    try:
        with os.scandir(codex_home) as entries:
            for index, entry in enumerate(entries):
                if index >= 1024:
                    break
                if not entry.name.endswith(".config.toml") or not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name.removesuffix(".config.toml")
                try:
                    agent_arguments("codex", name)
                except ValueError:
                    continue
                if name:
                    names["codex"].add(name)
    except OSError:
        pass
    return {harness: tuple(sorted(values)) for harness, values in names.items()}


class LaunchPicker(ModalScreen[LaunchChoice | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    LaunchPicker { align: center middle; }
    LaunchPicker > VerticalScroll {
        width: 64; max-width: 95%; height: auto; max-height: 95%;
        background: $surface; border: round $accent; padding: 1 2;
    }
    LaunchPicker Static { height: auto; margin-bottom: 1; }
    LaunchPicker Select, LaunchPicker Input { margin-bottom: 1; }
    LaunchPicker Horizontal { height: auto; }
    LaunchPicker Button { min-width: 10; margin-right: 1; }
    LaunchPicker #launch-error { color: $error; }
    """

    def __init__(self, project_label: str, harnesses: tuple[str, ...], current: str,
                 agents: dict[str, tuple[str, ...]], *, can_handoff: bool = False) -> None:
        super().__init__()
        self.project_label = project_label
        self.harnesses = harnesses
        self.current = current if current in harnesses else harnesses[0]
        self.agents = agents
        self.can_handoff = can_handoff

    def _options(self, harness: str) -> list[tuple[str, str]]:
        options = [("Default", "")]
        options.extend((name, name) for name in self.agents.get(harness, ()))
        if harness in {"opencode", "claude", "codex"}:
            options.append(("Other configured name…", "__custom__"))
        return options

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Choose harness and agent", markup=False)
            yield Static(self.project_label, markup=False)
            yield Static("Harness", markup=False)
            yield Select([(HARNESS_LABELS.get(name, name), name) for name in self.harnesses],
                         value=self.current, allow_blank=False, id="launch-harness")
            yield Static("Codex profile" if self.current == "codex" else "Agent", id="launch-agent-label")
            yield Select(self._options(self.current), value="", allow_blank=False, id="launch-agent")
            yield Input(placeholder="Configured agent or profile name", id="launch-custom")
            yield Static("New starts a fresh conversation in this project. Continue transfers the selected session's notes.", markup=False)
            yield Static("", id="launch-error", markup=False)
            with Horizontal():
                yield Button("New", variant="primary", id="launch-new")
                yield Button("Continue", id="launch-continue", disabled=not self.can_handoff)
                yield Button("Cancel", id="launch-cancel")

    def on_mount(self) -> None:
        self.query_one("#launch-custom", Input).display = False
        self.query_one("#launch-harness", Select).focus()

    @on(Select.Changed, "#launch-harness")
    def harness_changed(self, event: Select.Changed) -> None:
        if event.value not in self.harnesses:
            return
        self.query_one("#launch-agent-label", Static).update("Codex profile" if event.value == "codex" else "Agent")
        agent = self.query_one("#launch-agent", Select)
        agent.set_options(self._options(str(event.value)))
        agent.value = ""
        self.query_one("#launch-custom", Input).value = ""
        self.query_one("#launch-custom", Input).display = False

    @on(Select.Changed, "#launch-agent")
    def agent_changed(self, event: Select.Changed) -> None:
        custom = self.query_one("#launch-custom", Input)
        custom.display = event.value == "__custom__"
        if custom.display:
            custom.focus()

    @on(Button.Pressed)
    def submit(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "launch-cancel":
            self.action_cancel()
            return
        if event.button.id not in {"launch-new", "launch-continue"}:
            return
        harness = str(self.query_one("#launch-harness", Select).value)
        agent = str(self.query_one("#launch-agent", Select).value)
        if agent == "__custom__":
            agent = self.query_one("#launch-custom", Input).value.strip()
            if not agent:
                self.query_one("#launch-error", Static).update("Enter a configured name or choose Default.")
                return
        try:
            agent_arguments(harness, agent)
        except ValueError as error:
            self.query_one("#launch-error", Static).update(str(error))
            return
        self.dismiss(LaunchChoice(harness, agent, event.button.id == "launch-continue"))

    def action_cancel(self) -> None:
        self.dismiss(None)
