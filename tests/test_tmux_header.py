import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ocdeck.tmux_header import apply_header, header_commands, header_values, headers_for_sessions

HOSTILE = "#(touch {marker})x #[fg=red]y #{{session_name}} \x1b[31m‮"


class HeaderCommandTests(unittest.TestCase):
    def options(self, **kwargs):
        commands = header_commands("cc-abc", **{"harness": "claude", **kwargs})
        return {argv[4]: argv[5] for argv in commands}

    def test_header_for_each_builtin_harness(self):
        for harness, label in (("opencode", "OpenCode"), ("claude", "Claude Code"), ("codex", "Codex")):
            values = self.options(harness=harness, model="openai/gpt-6-astra#max", project="Deck", title="T")
            self.assertEqual(values["@ocdeck_harness"], label)
            self.assertEqual(values["@ocdeck_model"], "gpt-6-astra")
            self.assertEqual(values["status-position"], "top")
            self.assertIn("#{@ocdeck_title}", values["status-left"])

    def test_external_text_only_reaches_user_options_never_the_format(self):
        values = self.options(project="p#(id)", title="t#(id)", model="m#(id)", accent="#aabbcc;#(id)")
        for option in ("status-left", "status-right", "status-style", "pane-border-style"):
            self.assertNotIn("#(", values[option], option)
        self.assertIn("fg=#8ba4b5", values["status-style"])  # a non-hex accent falls back
        self.assertEqual(values["@ocdeck_title"], "t#(id)")  # kept literally, displayed literally

    def test_values_are_cleaned_and_clipped(self):
        values = header_values("claude", "", "a\x1b[31m\nb", "x" * 200)
        self.assertEqual(values["project"], "a [31m b")
        self.assertEqual(len(values["title"]), 80)
        self.assertEqual(values["model"], "model not reported")

    def test_only_managed_sessions_are_styled(self):
        calls = []
        run = lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0)
        self.assertFalse(apply_header("main", harness="claude", run=run))
        self.assertEqual(calls, [])
        self.assertTrue(apply_header("cx-1", harness="codex", run=run))
        self.assertTrue(calls and all(argv[3] == "=cx-1:" for argv in calls))

    def test_headers_for_live_sessions(self):
        session = SimpleNamespace(harness="claude", model="claude-opus-5-5", project_id="p", title="T",
                                  instance_count=1, terminals=("cc-1", "main"))
        found = list(headers_for_sessions([session], {"p": "Deck"}, {"p": "#5eead4"}))
        self.assertEqual([name for name, _ in found], ["cc-1"])
        self.assertEqual(found[0][1]["project"], "Deck")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class PrivateTmuxTests(unittest.TestCase):
    """Runs against a throwaway tmux server (own TMUX_TMPDIR), never the user's."""

    def test_hostile_titles_render_literally_and_run_nothing(self):
        with tempfile.TemporaryDirectory() as base:
            marker = Path(base) / "pwned"
            control = Path(base) / "control"
            env = {**os.environ, "TMUX_TMPDIR": base, "TERM": "xterm-256color"}
            env.pop("TMUX", None)
            command = ["tmux", "-S", str(Path(base) / "test.sock"), "-f", "/dev/null"]
            tmux = lambda *args: subprocess.run([*command, *args], env=env, capture_output=True, text=True, timeout=10)
            master, slave = pty.openpty()
            client = None

            def render_for(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if select.select([master], [], [], max(0, min(0.1, end - time.monotonic())))[0]:
                        os.read(master, 65536)

            try:
                self.assertEqual(tmux("new-session", "-d", "-s", "cc-probe", "-x", "120", "-y", "5", "sleep 30").returncode, 0)
                run = lambda argv, **kwargs: subprocess.run([*command, *argv[1:]], env=env, **kwargs)
                self.assertFalse(apply_header("cc-pro", harness="claude", run=run),
                                 "a prefix must not match an existing terminal")
                tmux("set-option", "-t", "=cc-probe:", "status-interval", "1")
                tmux("set-option", "-t", "=cc-probe:", "status-left", f"#(touch {control})")
                client = subprocess.Popen([*command, "attach-session", "-t", "=cc-probe"], env=env,
                                          stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                render_for(2)
                self.assertIsNone(client.poll())
                self.assertTrue(control.exists(), "the attached client must actually execute status jobs")
                title = HOSTILE.format(marker=marker)
                self.assertTrue(apply_header("cc-probe", harness="claude", model="claude-opus-5-5",
                                             project=title, title=title, accent="#5eead4", run=run))
                tmux("set-option", "-t", "=cc-probe:", "status-interval", "1")
                render_for(2)
                rendered = tmux("display-message", "-p", "-t", "=cc-probe:", "#{T:status-left}").stdout
                self.assertFalse(marker.exists(), "a #( ) command in a title was executed")
                self.assertIn("#(touch", rendered)
                self.assertIn("Claude Code", rendered)
            finally:
                tmux("kill-server")  # the private server only
                if client is not None:
                    client.wait(timeout=5)
                os.close(slave)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
