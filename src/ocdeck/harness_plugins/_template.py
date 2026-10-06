"""Template for adding an agent harness to OC Deck.

Copy this file to ``<harness>.py`` in this folder (a name without a leading
underscore), fill in the marked parts, and restart OC Deck. The harness then
appears everywhere automatically: session list and Agents board (badge, RUNTIME
code), Shift+H launching, `o` resume/attach, `z z` purge, Shift+L history,
lineage nesting, handoff, and `ocdeck-hub status`.

Checklist
1. HarnessSpec: a unique id, a two-letter code, a unique tmux prefix, and
   the CLI names.
2. Adapter: tell OC Deck where the CLI keeps its transcripts, how to parse
   one, and how to start or resume a session.
3. Tests: add tests/test_harness_<id>.py with a small transcript fixture
   (see tests/test_harnesses.py for Claude and Codex examples).
4. Optional: config sync for ocdeck-hub. Add a planner to
   ``ocdeck.hub.SYNC_PLANNERS[<id>]``; without one the hub skips the harness.

Every harness is optional: the user enables it with "on"/"off"/"auto" in
``~/.config/ocdeck/harnesses.json`` (or ``ocdeck --harness``).
"""

from __future__ import annotations

from pathlib import Path

from ocdeck.harnesses import (
    HarnessSpec,
    LiveProcess,
    TranscriptHarness,
    TranscriptInfo,
    find_binary,
    register_harness,
)


class ExampleHarness(TranscriptHarness):
    harness = "example"  # must equal HarnessSpec.id below

    def __init__(self, root: Path | None = None, binary: str | None = None) -> None:
        # TODO: where the CLI stores one file per session.
        super().__init__(root or Path.home() / ".example/sessions", binary or find_binary(self.harness))

    def transcript_files(self) -> list[Path]:
        # TODO: every transcript file; OC Deck keeps the newest 200.
        try:
            return list(self.root.glob("*.jsonl"))
        except OSError:
            return []

    def parse_transcript(self, path: Path, size: int) -> TranscriptInfo | None:
        # TODO: return None for files that are not sessions. Required: the
        # native session id and the working directory. Set prompt_ms/done_ms/
        # turn_open when the format marks turn boundaries; RUNNING/REVIEW
        # status then follows the same rules as Claude and OpenCode.
        return None

    def process_session_id(self, process: LiveProcess) -> str:
        # TODO: the session id when it is on the command line (e.g. --resume ID);
        # "" otherwise (OC Deck then matches the process by working directory).
        return ""

    def resume_command(self, session_id: str, directory: str, *, browser: bool = False) -> list[str]:
        # TODO: argv that reopens an existing session interactively.
        return [self.binary or self.harness, "--resume", session_id]

    def new_command(
        self, directory: str, prompt: str = "", *, browser: bool = False
    ) -> tuple[list[str], str]:
        # TODO: argv for a new session, plus its id if you can choose it up front
        # ("" if the CLI assigns it; the grant/lineage then attach once it appears).
        return [self.binary or self.harness, *([prompt] if prompt else [])], ""


# This template registers nothing: the name starts with "_", so it is never loaded.
# In your copy, uncomment and adjust:
#
# register_harness(HarnessSpec(
#     id="example", label="Example CLI", code="EX", binaries=("example",),
#     tmux_prefix="ex", badge="EX", style="#c0b6ff", adapter=ExampleHarness,
# ))
