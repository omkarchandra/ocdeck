"""Harness REGISTRY and cross-harness LINEAGE tests for :mod:`ocdeck.harnesses`.

Hermetic by construction:

* ``HOME``, ``XDG_CONFIG_HOME``, ``XDG_STATE_HOME``, ``XDG_RUNTIME_DIR``,
  ``OCDECK_HUB_DIR`` and ``CODEX_HOME`` point at a throwaway ``tempfile`` tree,
  so the real ``~/.config``, ``~/.claude``, ``~/.codex`` and ``~/.local`` are
  never read or written.
* ``/proc`` is faked with temporary directories (per-pid ``stat``, ``cmdline``,
  ``cwd`` symlink and ``environ`` plus a ``stat`` file carrying ``btime``); the
  only real ``/proc``/``PATH`` access is stubbed out.
* No subprocess, tmux, D-Bus, terminal or agent process is ever started.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocdeck import harness_plugins, harnesses
from ocdeck.agent_tabs import is_managed_session
from ocdeck.harnesses import (
    LINEAGE_LIMIT, HarnessSpec, ProcessInfo, TranscriptHarness, TranscriptInfo,
    apply_lineage, build_adapters, default_lineage_file, harness_ids,
    headless_child_harness, load_harness_plugins, load_lineage, managed_tmux_prefixes,
    observe_lineage, parent_stamp, read_process_table, register_harness,
    split_session_key, unregister_harness,
)
from ocdeck.models import DashboardSnapshot, SessionRecord

# --- fakes -------------------------------------------------------------------

LIVE_TABLES = (
    harnesses.HARNESS_LABELS, harnesses.HARNESS_BADGES, harnesses.HARNESS_CODES,
    harnesses.HARNESS_BINARIES, harnesses.HARNESS_STYLES,
)


class FakeAdapter(TranscriptHarness):
    """Adapter that reads nothing, so registry wiring can be checked in isolation."""

    harness = "fake"

    def __init__(self, root=None, binary=None):
        super().__init__(Path(root or "/nonexistent"), binary)

    def transcript_files(self):
        return []

    def parse_transcript(self, path, size):
        return TranscriptInfo("x", "/w", "t", 1, 2)

    def process_session_id(self, process):
        return ""

    def resume_command(self, session_id, directory, *, browser=False):
        return ["fake", "--resume", session_id]

    def new_command(self, directory, prompt="", *, browser=False):
        return ["fake"], ""


def spec(harness="gemini", **overrides) -> HarnessSpec:
    fields = {
        "label": f"{harness} CLI", "code": (harness[:2] or "zz").upper(),
        "binaries": (harness,), "tmux_prefix": harness[:2] or "zz",
        "badge": (harness[:2] or "zz").upper(), "style": "#123456", "adapter": None,
    }
    fields.update(overrides)
    return HarnessSpec(harness, **fields)


def session(session_id, directory="/w", created_ms=0, harness="opencode", **kw) -> SessionRecord:
    return SessionRecord(
        id=session_id, title=f"title {session_id}", directory=directory, project_id="p",
        created_ms=created_ms, updated_ms=created_ms + 1000, harness=harness, **kw,
    )


def snapshot(*sessions) -> DashboardSnapshot:
    return DashboardSnapshot(sessions=tuple(sessions))


# --- fake /proc --------------------------------------------------------------

BTIME = 1_000_000  # seconds, as reported by the fake /proc/stat btime line


def proc_stat_line(pid: int, ppid: int, starttime: int, comm: str = "agent") -> str:
    """A ``/proc/<pid>/stat`` line with the kernel's 1-indexed field layout.

    Field 3 is ``state`` and field 22 is ``starttime``; the parser slices after
    the last ``)`` of ``comm``, so ``comm`` may contain spaces and parentheses.
    """
    fields = ["0"] * 52
    fields[0], fields[2], fields[3], fields[21] = str(pid), "S", str(ppid), str(starttime)
    return f"{fields[0]} ({comm}) " + " ".join(fields[2:]) + "\n"


def start_ticks(start_ms: float, btime: int = BTIME) -> int:
    return int((start_ms / 1000 - btime) * os.sysconf("SC_CLK_TCK"))


def fake_proc(root: Path, processes: dict, *, btime: int = BTIME, btime_line: bool = True) -> Path:
    """Build a fake ``/proc``: ``{pid: {ppid, start_ms, cwd, args, env, comm}}``."""
    root.mkdir(parents=True, exist_ok=True)
    if btime_line:
        (root / "stat").write_text(
            f"cpu  1 2 3 4\nbtime {btime}\ncpu0 1 2 3\n", encoding="utf-8")
    for pid, info in processes.items():
        folder = root / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "stat").write_text(proc_stat_line(
            pid, info.get("ppid", 1),
            start_ticks(info.get("start_ms", btime * 1000), btime), info.get("comm", "agent"),
        ), encoding="utf-8")
        (folder / "cmdline").write_bytes(
            b"".join(str(arg).encode() + b"\0" for arg in info.get("args", ("agent",))))
        if info.get("env") is not None:
            (folder / "environ").write_bytes(
                b"".join(f"{key}={value}".encode() + b"\0" for key, value in info["env"].items()))
        if "cwd" in info:
            (folder / "cwd").symlink_to(info["cwd"])
    return root


def process(pid, ppid, start_ms, cwd, args) -> ProcessInfo:
    return ProcessInfo(pid=pid, ppid=ppid, start_ms=start_ms, cwd=cwd, args=tuple(args))


# --- base test cases ---------------------------------------------------------

class Hermetic(unittest.TestCase):
    """Point every state directory at a throwaway tree."""

    ENVIRONMENTS = (
        "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
        "OCDECK_HUB_DIR", "CODEX_HOME",
    )

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="ocdeck-registry-lineage-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        environ = {name: str(self.base / name.lower()) for name in self.ENVIRONMENTS}
        for value in environ.values():
            Path(value).mkdir(parents=True, exist_ok=True)
        environ["HOME"] = str(Path(environ["HOME"]) / "home")
        Path(environ["HOME"]).mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = Path(os.environ["HOME"])
        self.state_home = Path(os.environ["XDG_STATE_HOME"])
        self.paths = self.base / "paths"
        self.paths.mkdir(parents=True, exist_ok=True)


class RegistryCase(Hermetic):
    """Registry tests that always put the registry back the way they found it."""

    def line(self, index: int):
        """A unique, valid (code, tmux_prefix) pair for iteration ``index``."""
        return f"{index % 100:02d}", f"z{index % 10000:04d}"

    def setUp(self):
        super().setUp()
        self.originals = {name: harnesses.HARNESS_REGISTRY[name] for name in harness_ids()}

    def restore(self, harness: str) -> None:
        original = self.originals.get(harness)
        if original is None:
            unregister_harness(harness)
        else:
            register_harness(original)

    def forget(self, harness: str) -> None:
        """Undo any change to ``harness`` when the test ends."""
        self.addCleanup(self.restore, harness)

    def register(self, spec: HarnessSpec) -> HarnessSpec:
        self.forget(spec.id)
        return register_harness(spec)

    def assert_live_tables(self, spec: HarnessSpec) -> None:
        self.assertIn(spec.id, harness_ids())
        self.assertIn(spec.id, harnesses.HARNESS_IDS)
        for table in LIVE_TABLES:
            self.assertIn(spec.id, table)
        self.assertEqual(harnesses.HARNESS_LABELS[spec.id], spec.label)
        self.assertEqual(harnesses.HARNESS_BADGES[spec.id], spec.badge)
        self.assertEqual(harnesses.HARNESS_CODES[spec.id], spec.code)
        self.assertEqual(harnesses.HARNESS_BINARIES[spec.id], tuple(spec.binaries))
        self.assertEqual(harnesses.HARNESS_STYLES[spec.id], spec.style)

    def assert_gone(self, harness: str) -> None:
        self.assertNotIn(harness, harness_ids())
        self.assertNotIn(harness, harnesses.HARNESS_IDS)
        for table in LIVE_TABLES:
            self.assertNotIn(harness, table)


# --- registry: spec validation ------------------------------------------------

class RegistryValidationTests(RegistryCase):
    def test_ids_are_lowercase_words_of_2_to_20_characters(self):
        for index, good in enumerate(("ab", "a_1", "zz9", "a" * 20)):
            with self.subTest(id=good):
                code, prefix = self.line(index)
                self.register(spec(good, code=code, tmux_prefix=prefix))
                self.assert_live_tables(harnesses.HARNESS_REGISTRY[good])
        for index, bad in enumerate(("", "a", "a" * 21, "Gemini", "1gem", "gem-in", "gem ini", "gem.in", "_gem")):
            with self.subTest(id=bad):
                code, prefix = self.line(100 + index)
                self.forget(bad)
                with self.assertRaises(ValueError):
                    register_harness(spec(bad, code=code, tmux_prefix=prefix))
                self.assert_gone(bad)

    def test_codes_are_exactly_two_capital_letters_or_digits(self):
        for index, good in enumerate(("GM", "G1", "00", "ZZ")):
            with self.subTest(code=good):
                _, prefix = self.line(200 + index)
                self.register(spec(f"ok{index}", code=good, tmux_prefix=prefix))
        for index, bad in enumerate(("", "G", "GMX", "gm", "G-", "G M", "G1x")):
            with self.subTest(code=bad):
                _, prefix = self.line(300 + index)
                self.forget(f"no{index}")
                with self.assertRaises(ValueError):
                    register_harness(spec(f"no{index}", code=bad, tmux_prefix=prefix))
        self.assertNotIn("no0", harness_ids())

    def test_tmux_prefixes_are_short_lowercase_and_never_reserved(self):
        for index, good in enumerate(("z", "ab", "cc2", "z9x9z", "abcde")):
            with self.subTest(prefix=good):
                code, _ = self.line(400 + index)
                self.register(spec(f"p{index}", code=code, tmux_prefix=good))
        for index, bad in enumerate(("", "oc", "oc2", "o-t", "1z", "Z", "abcdefg", "z_", "z.a", "o c")):
            with self.subTest(prefix=bad):
                code, _ = self.line(500 + index)
                self.forget(f"q{index}")
                with self.assertRaises(ValueError):
                    register_harness(spec(f"q{index}", code=code, tmux_prefix=bad))
                self.assert_gone(f"q{index}")
        self.assertIn("oc-", managed_tmux_prefixes())
        self.assertIn("oc2-", managed_tmux_prefixes())

    def test_a_prefix_or_code_used_by_another_harness_is_refused(self):
        code, prefix = self.line(600)
        self.register(spec("zzalpha", code=code, tmux_prefix=prefix))
        other = "zzbeta"
        self.forget(other)
        with self.assertRaises(ValueError) as clash:
            register_harness(spec(other, code="QQ", tmux_prefix=prefix))
        self.assertIn("zzalpha", str(clash.exception))
        self.assert_gone(other)
        with self.assertRaises(ValueError) as clash:
            register_harness(spec(other, code=code, tmux_prefix="zzoth"))
        self.assertIn("code", str(clash.exception))
        # A free code *and* a free prefix is accepted.
        self.register(spec(other, code="QQ", tmux_prefix="zzoth"))
        self.assert_live_tables(harnesses.HARNESS_REGISTRY[other])
        # A near-miss prefix ("zzalp") is not a clash.
        self.register(spec("zzgamma", code="WW", tmux_prefix=prefix + "9"))
        self.assert_live_tables(harnesses.HARNESS_REGISTRY["zzgamma"])

    def test_replacing_the_builtin_opencode_spec_is_still_validated(self):
        """BUG: harnesses.py:63 - only the id and code checks run for ``id == "opencode"``.

        The tmux-prefix format check and the clash check live inside
        ``if spec.id != "opencode"``, so any module that registers a spec with
        the id "opencode" can replace the built-in with a reserved ("oc"),
        malformed ("o-t", "abcdefg") or clashing ("cc", which makes
        ``managed_tmux_prefixes()`` report "cc-" twice and mis-attribute Claude
        terminals) prefix, an empty binary list (``find_binary("opencode")``
        returns None, so OC Deck can no longer launch OpenCode) and a foreign
        label/badge/style. ``unregister_harness`` refuses to remove the built-in
        ("OpenCode is built in"), so silently clobbering it is the one way left
        to break it. Fix: keep the id/code checks for every spec, and for
        ``id == "opencode"`` require ``tmux_prefix == ""`` (the built-in's own
        shape) and run the clash check on any non-empty prefix.
        """
        self.forget("opencode")
        with self.assertRaises(ValueError):
            register_harness(spec("opencode", code="R9", tmux_prefix="cc"))
        self.assertIs(harnesses.HARNESS_REGISTRY["opencode"], self.originals["opencode"])

    def test_opencode_cannot_be_unregistered(self):
        original = harnesses.HARNESS_REGISTRY["opencode"]
        with self.assertRaises(ValueError):
            unregister_harness("opencode")
        self.assertIs(harnesses.HARNESS_REGISTRY["opencode"], original)
        self.assertEqual(harnesses.HARNESS_CODES["opencode"], "OC")

    def test_unregistering_an_unknown_harness_is_a_noop(self):
        before = harness_ids()
        unregister_harness("not-a-harness")
        self.assertEqual(harness_ids(), before)
        self.assertEqual(harnesses.HARNESS_IDS, harness_ids())


# --- registry: live tables and updates ---------------------------------------

class LiveTableTests(RegistryCase):
    def test_registering_fills_every_live_table(self):
        code, prefix = self.line(700)
        added = self.register(spec("zzdelta", code=code, tmux_prefix=prefix, adapter=FakeAdapter))
        self.assert_live_tables(added)
        self.assertEqual(harnesses.HARNESS_IDS, harness_ids())
        self.assertEqual(harnesses.HARNESS_IDS[-1], "zzdelta")
        self.assertIn(f"{prefix}-", managed_tmux_prefixes())
        self.assertTrue(is_managed_session(f"{prefix}-session"))
        self.assertEqual(harnesses.runtime_label("zzdelta", "zz-1", full=True), "zzdelta CLI · zz-1")
        self.assertEqual(harnesses.runtime_label("zzdelta", "zz-1", full=False), f"{code} ZZ")

    def test_unregistering_empties_every_live_table(self):
        code, prefix = self.line(710)
        self.register(spec("zzdelta", code=code, tmux_prefix=prefix))
        unregister_harness("zzdelta")
        self.assert_gone("zzdelta")
        self.assertNotIn(f"{prefix}-", managed_tmux_prefixes())
        self.assertFalse(is_managed_session(f"{prefix}-session"))
        self.assertTrue(is_managed_session("cc-1"))  # built-ins are untouched
        self.assertEqual(split_session_key("zzdelta:x"), ("opencode", "zzdelta:x"))

    def test_tables_are_mutated_in_place_so_holders_see_new_harnesses(self):
        labels, styles = harnesses.HARNESS_LABELS, harnesses.HARNESS_STYLES
        code, prefix = self.line(720)
        added = self.register(spec("zzdelta", code=code, tmux_prefix=prefix, style="#0a0b0c"))
        self.assertIs(harnesses.HARNESS_LABELS, labels)
        self.assertIs(harnesses.HARNESS_STYLES, styles)
        self.assertEqual(labels["zzdelta"], added.label)
        self.assertEqual(styles["zzdelta"], "#0a0b0c")

    def test_re_registering_the_same_id_with_a_changed_prefix_moves_it(self):
        code, old = self.line(730)
        self.register(spec("zzdelta", code=code, tmux_prefix=old, label="First", badge="F1"))
        first_index = harness_ids().index("zzdelta")
        self.assertIn(f"{old}-", managed_tmux_prefixes())
        # Same id, new prefix/code/label: an update, not a second harness.
        updated = self.register(spec(
            "zzdelta", code="R9", tmux_prefix="zznew", label="Second", badge="R9", style="#654321"))
        self.assert_live_tables(updated)
        self.assertEqual(harness_ids().count("zzdelta"), 1)
        self.assertEqual(harness_ids().index("zzdelta"), first_index)
        self.assertNotIn(f"{old}-", managed_tmux_prefixes())
        self.assertIn("zznew-", managed_tmux_prefixes())
        self.assertTrue(is_managed_session("zznew-1"))
        self.assertFalse(is_managed_session(f"{old}-1"))
        self.assertEqual(split_session_key("zzdelta:x"), ("zzdelta", "x"))
        # The freed prefix is available again.
        self.register(spec("zzepsilon", code="R8", tmux_prefix=old))
        self.assertIn(f"{old}-", managed_tmux_prefixes())

    def test_unregistering_then_re_registering_moves_the_id_to_the_end(self):
        self.register(spec("zzdelta", code=self.line(740)[0], tmux_prefix=self.line(740)[1]))
        self.register(spec("zzepsilon", code=self.line(741)[0], tmux_prefix=self.line(741)[1]))
        self.assertLess(harness_ids().index("zzdelta"), harness_ids().index("zzepsilon"))
        unregister_harness("zzdelta")
        self.assert_gone("zzdelta")
        self.register(spec("zzdelta", code=self.line(742)[0], tmux_prefix=self.line(742)[1]))
        self.assertEqual(harness_ids()[-1], "zzdelta")
        self.assertEqual(harnesses.HARNESS_IDS, harness_ids())
        self.assert_live_tables(harnesses.HARNESS_REGISTRY["zzdelta"])

    def test_managed_prefixes_are_recomputed_and_keep_the_opencode_ones(self):
        self.assertEqual(managed_tmux_prefixes()[:2], ("oc-", "oc2-"))
        self.assertNotIn("oc-", [prefix for prefix in managed_tmux_prefixes()[2:]])
        # opencode's spec has no tmux prefix of its own, so it adds nothing.
        code, prefix = self.line(750)
        self.register(spec("zzdelta", code=code, tmux_prefix=prefix))
        self.assertEqual(managed_tmux_prefixes().count(f"{prefix}-"), 1)
        unregister_harness("zzdelta")
        self.assertEqual(managed_tmux_prefixes().count(f"{prefix}-"), 0)

    def test_find_binary_follows_the_live_binaries_table(self):
        local = self.home / ".local/bin"
        local.mkdir(parents=True, exist_ok=True)
        for name in ("zzlocal", "zzgone"):
            tool = local / name
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)
        self.register(spec("zzdelta", code=self.line(760)[0], tmux_prefix=self.line(760)[1],
                           binaries=("zzlocal", "zzgone")))
        self.register(spec("zznone", code=self.line(761)[0], tmux_prefix=self.line(761)[1],
                           binaries=("zznope",)))
        with mock.patch("ocdeck.harnesses.shutil.which", return_value=None):
            self.assertEqual(harnesses.find_binary("zzdelta"), str(local / "zzlocal"))
            self.assertIsNone(harnesses.find_binary("zznone"))
        # PATH is searched first, for every name, before the install directories.
        with mock.patch("ocdeck.harnesses.shutil.which",
                        side_effect=lambda name: "/usr/bin/zzgone" if name == "zzgone" else None):
            self.assertEqual(harnesses.find_binary("zzdelta"), "/usr/bin/zzgone")
        # Re-registering with other binaries retires the old search.
        self.register(spec("zzdelta", code=self.line(762)[0], tmux_prefix=self.line(762)[1],
                           binaries=("zzother",)))
        with mock.patch("ocdeck.harnesses.shutil.which", return_value=None):
            self.assertIsNone(harnesses.find_binary("zzdelta"))
        unregister_harness("zzdelta")
        with mock.patch("ocdeck.harnesses.shutil.which", return_value=None):
            self.assertIsNone(harnesses.find_binary("zzdelta"))


# --- registry: session keys and adapters -------------------------------------

class SessionKeyAndAdapterTests(RegistryCase):
    def test_split_session_key_only_splits_registered_foreign_harnesses(self):
        self.assertEqual(split_session_key("claude:abc"), ("claude", "abc"))
        self.assertEqual(split_session_key("codex:0198-x"), ("codex", "0198-x"))
        self.assertEqual(split_session_key("ses_abc"), ("opencode", "ses_abc"))
        self.assertEqual(split_session_key("opencode:ses_abc"), ("opencode", "opencode:ses_abc"))
        self.assertEqual(split_session_key("weird:abc"), ("opencode", "weird:abc"))
        self.assertEqual(split_session_key("claude:a:b"), ("claude", "a:b"))
        self.assertEqual(split_session_key("claude:"), ("claude", ""))
        self.assertEqual(split_session_key(""), ("opencode", ""))

    def test_split_session_key_follows_the_registry(self):
        code, prefix = self.line(770)
        self.register(spec("zzdelta", code=code, tmux_prefix=prefix))
        self.assertEqual(split_session_key("zzdelta:abc"), ("zzdelta", "abc"))
        unregister_harness("zzdelta")
        self.assertEqual(split_session_key("zzdelta:abc"), ("opencode", "zzdelta:abc"))

    def test_build_adapters_skips_harnesses_without_an_adapter(self):
        with mock.patch("ocdeck.harnesses.find_binary", return_value=None):
            adapters = build_adapters(["opencode"])
            self.assertEqual(adapters, [])
            self.assertEqual(build_adapters([]), [])
            self.assertEqual(build_adapters(["not-a-harness"]), [])
            adapters = build_adapters(["claude", "codex"])
            self.assertEqual([type(a).__name__ for a in adapters], ["ClaudeHarness", "CodexHarness"])
            # The caller's order wins, not the registration order.
            self.assertEqual(
                [type(a).__name__ for a in build_adapters(["codex", "claude"])],
                ["CodexHarness", "ClaudeHarness"],
            )
        code, prefix = self.line(780)
        self.register(spec("zzdelta", code=code, tmux_prefix=prefix, adapter=FakeAdapter))
        self.assertEqual([type(a) for a in build_adapters(["zzdelta"])], [FakeAdapter])
        unregister_harness("zzdelta")
        self.assertEqual(build_adapters(["zzdelta"]), [])

    def test_a_plugin_adapter_takes_its_tmux_prefix_from_its_spec(self):
        code, prefix = self.line(790)
        self.register(spec("zzdelta", code=code, tmux_prefix=prefix, adapter=FakeAdapter))
        adapter = FakeAdapter()
        adapter.harness = "zzdelta"
        self.assertEqual(adapter.tmux_prefix, prefix)
        self.assertEqual(adapter.tmux_name("abc"), f"{prefix}-abc")
        self.assertEqual(adapter.session_key("abc"), "zzdelta:abc")
        # A spec without a prefix falls back to the harness id's first letters.
        self.register(spec("zzepsilon", code=self.line(791)[0], tmux_prefix="zze",
                           adapter=FakeAdapter))
        adapter.harness = "zznotspec"
        self.assertEqual(adapter.tmux_prefix, "zz")


# --- registry: plugin loading -------------------------------------------------

class PluginLoadingTests(RegistryCase):
    def write_plugins(self, **modules: str) -> Path:
        folder = self.paths / f"plugins-{len(list(self.paths.glob('plugins-*')))}"
        folder.mkdir(parents=True, exist_ok=True)
        for name, source in modules.items():
            (folder / f"{name}.py").write_text(source, encoding="utf-8")
            self.addCleanup(sys.modules.pop, f"ocdeck.harness_plugins.{name}", None)
        return folder

    def load(self, folder: Path):
        stderr = io.StringIO()
        with mock.patch.object(harness_plugins, "__path__", [str(folder)]), \
                contextlib.redirect_stderr(stderr):
            loaded = load_harness_plugins()
        return loaded, stderr.getvalue()

    def test_a_good_plugin_registers_and_bad_ones_are_only_warned_about(self):
        self.forget("plugok")
        folder = self.write_plugins(
            plugok=(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugok', 'Plug OK', 'K1', ('plugok',),\n"
                "                                tmux_prefix='pk', adapter=None))\n"
            ),
            plugboom="raise RuntimeError('boom')\n",
            plugsyntax="def broken(:\n",
            plugclash=(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('clashy', 'Clash', 'CC', ('clashy',),"
                " tmux_prefix='cl'))\n"
            ),
        )
        (folder / "not-a-module.txt").write_text("raise RuntimeError('never read')\n", encoding="utf-8")
        loaded, errors = self.load(folder)
        self.assertEqual(loaded, ["plugok"])
        self.assertIn("plugok", harness_ids())
        self.assertEqual(harnesses.HARNESS_CODES["plugok"], "K1")
        self.assertIn("pk-", managed_tmux_prefixes())
        # The clashing plugin is skipped and leaves no trace in the registry.
        self.assertNotIn("clashy", harness_ids())
        self.assertNotIn("clashy", harnesses.HARNESS_LABELS)
        self.assertIn("plugboom failed to load: boom", errors)
        self.assertIn("plugsyntax failed to load:", errors)
        self.assertIn("plugclash failed to load:", errors)
        self.assertIn("claude", errors)  # the clash names the harness that owns the code
        self.assertNotIn("not-a-module", errors)

    def test_modules_starting_with_an_underscore_are_never_imported(self):
        self.forget("plugsecret")
        folder = self.write_plugins(
            _plughidden=(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugsecret', 'Secret', 'S1', ('s',),"
                " tmux_prefix='ps'))\n"
            ),
            plugok="pass\n",
        )
        loaded, errors = self.load(folder)
        self.assertEqual(loaded, ["plugok"])
        self.assertEqual(errors, "")
        self.assertNotIn("plugsecret", harness_ids())
        self.assertNotIn("ocdeck.harness_plugins._plughidden", sys.modules)
        # The shipped template is documentation, never a harness.
        self.assertNotIn("example", harness_ids())
        self.assertNotIn("ocdeck.harness_plugins._template", sys.modules)

    def test_a_plugin_re_registering_a_builtin_replaces_it_everywhere(self):
        self.forget("claude")
        original = self.originals["claude"]
        folder = self.write_plugins(
            plugrogue=(
                "from ocdeck.harnesses import HarnessSpec, TranscriptHarness, TranscriptInfo,"
                " register_harness\n"
                "class Rogue(TranscriptHarness):\n"
                "    harness = 'claude'\n"
                "    def __init__(self, root=None, binary=None):\n"
                "        super().__init__(root, binary)\n"
                "    def transcript_files(self):\n"
                "        return []\n"
                "    def parse_transcript(self, path, size):\n"
                "        return TranscriptInfo('x', '/w', 't', 1, 2)\n"
                "    def process_session_id(self, process):\n"
                "        return ''\n"
                "    def resume_command(self, session_id, directory, *, browser=False):\n"
                "        return ['claude']\n"
                "    def new_command(self, directory, prompt='', *, browser=False):\n"
                "        return ['claude'], ''\n"
                "register_harness(HarnessSpec('claude', 'Claude Rogue', 'R9', ('claude',),\n"
                "        tmux_prefix='cg', badge='R9', style='#ff0000', adapter=Rogue))\n"
            ),
        )
        loaded, errors = self.load(folder)
        self.assertEqual((loaded, errors), (["plugrogue"], ""))
        self.assertEqual(harnesses.HARNESS_LABELS["claude"], "Claude Rogue")
        self.assertEqual(harnesses.HARNESS_BADGES["claude"], "R9")
        self.assertEqual(harnesses.HARNESS_CODES["claude"], "R9")
        self.assertEqual(harnesses.HARNESS_BINARIES["claude"], ("claude",))
        self.assertEqual(harnesses.HARNESS_STYLES["claude"], "#ff0000")
        self.assertIn("cg-", managed_tmux_prefixes())
        self.assertNotIn("cc-", managed_tmux_prefixes())
        self.assertFalse(is_managed_session("cc-1"))
        self.assertTrue(is_managed_session("cg-1"))
        self.assertEqual(split_session_key("claude:abc"), ("claude", "abc"))
        self.assertEqual([type(a).__name__ for a in build_adapters(["claude"])], ["Rogue"])
        # Re-registering the built-in's own spec restores it, prefix included.
        self.restore("claude")
        self.assertIs(harnesses.HARNESS_REGISTRY["claude"], original)
        self.assertIn("cc-", managed_tmux_prefixes())
        self.assertEqual(harnesses.HARNESS_LABELS["claude"], original.label)
        self.assertEqual([type(a).__name__ for a in build_adapters(["claude"])], ["ClaudeHarness"])

    def test_an_already_imported_plugin_is_not_executed_twice(self):
        marker = self.paths / "runs.txt"
        self.forget("plugok")
        folder = self.write_plugins(
            plugok=(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).open('a').write('ran\\n')\n"
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugok', 'Plug OK', 'K1', ('plugok',),"
                " tmux_prefix='pk'))\n"
            ),
        )
        first, errors = self.load(folder)
        second, again = self.load(folder)
        self.assertEqual((first, errors), (["plugok"], ""))
        self.assertEqual((second, again), (["plugok"], ""))
        self.assertEqual(marker.read_text(encoding="utf-8"), "ran\n")
        self.assertEqual(harness_ids().count("plugok"), 1)

    def test_a_plugin_that_exits_does_not_take_the_deck_down(self):
        """BUG: harnesses.py:1350 - only ``Exception`` is caught around a plugin import.

        ``load_harness_plugins`` promises "a broken plugin is skipped with a
        warning, never fatal" and runs at import time (harnesses.py:1355), but
        the ``except Exception`` does not catch ``SystemExit`` (nor
        ``KeyboardInterrupt``): a plugin module that calls ``sys.exit()`` while
        being imported - an argparse call, a startup self-check, a wrapper that
        re-executes itself - raises SystemExit straight through
        ``load_harness_plugins`` and kills OC Deck during startup. Fix: catch
        ``SystemExit`` there as well (or ``BaseException``).
        """
        folder = self.write_plugins(plugexit="import sys\nsys.exit(3)\n")
        try:
            loaded, errors = self.load(folder)
        except BaseException as error:  # noqa: BLE001 - the point of the test
            self.fail(f"a broken plugin must not break the deck, got {type(error).__name__}")
        self.assertEqual(loaded, [])
        self.assertIn("plugexit failed to load", errors)

    def test_several_good_plugins_all_load(self):
        for index, name in enumerate(("plugone", "plugtwo")):
            self.forget(name)
        folder = self.write_plugins(
            plugone=(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugone', 'One', 'N1', ('one',),"
                " tmux_prefix='po'))\n"
            ),
            plugtwo=(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugtwo', 'Two', 'N2', ('two',),"
                " tmux_prefix='pt'))\n"
            ),
        )
        loaded, errors = self.load(folder)
        self.assertEqual(sorted(loaded), ["plugone", "plugtwo"])
        self.assertEqual(errors, "")
        self.assertIn("po-", managed_tmux_prefixes())
        self.assertIn("pt-", managed_tmux_prefixes())


# --- lineage: the process table ----------------------------------------------

class ProcessTableTests(Hermetic):
    def test_a_fake_proc_tree_yields_ppid_start_time_cwd_and_argv(self):
        self.assertEqual(os.sysconf("SC_CLK_TCK"), 100, "fixtures assume USER_HZ 100")
        root = fake_proc(self.paths / "proc", {
            10: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=("claude", "--resume", "p1")),
            20: dict(ppid=10, start_ms=1_500_000, cwd="/w/a", args=("opencode2", "run", "go"),
                     comm="(na)me)"),
            30: dict(ppid=1, start_ms=2_250_000, cwd="/w/b", args=("codex", "exec")),
        })
        table = read_process_table(root)
        self.assertEqual(sorted(table), [10, 20, 30])
        self.assertEqual(table[20].ppid, 10)
        self.assertEqual(table[20].cwd, "/w/a")
        self.assertEqual(table[20].args, ("opencode2", "run", "go"))
        self.assertEqual(table[20].start_ms, 1_500_000)
        self.assertEqual(table[10].args, ("claude", "--resume", "p1"))
        self.assertEqual(table[30].start_ms, 2_250_000)
        self.assertEqual(table[30].cwd, "/w/b")

    def test_odd_proc_entries_are_skipped(self):
        root = fake_proc(self.paths / "proc", {
            10: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=("claude",)),
            20: dict(ppid=1, start_ms=1_000_000, args=("kernel",)),          # no cwd link
            30: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=()),         # kernel thread
        })
        (root / "40").mkdir()
        (root / "40" / "stat").write_text("40 (broken S 1 0\n", encoding="utf-8")  # no ')'
        (root / "40" / "cmdline").write_bytes(b"x\0")
        (root / "40" / "cwd").symlink_to("/w")
        for name in ("self", "net", "sys", "meminfo"):
            (root / name).mkdir(exist_ok=True)
        table = read_process_table(root)
        self.assertEqual(sorted(table), [10])
        self.assertNotIn(20, table)
        self.assertNotIn(30, table)
        self.assertNotIn(40, table)

    def test_cmdline_separators_and_bad_bytes_are_tolerated(self):
        root = fake_proc(self.paths / "proc", {
            10: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=("claude",)),
        })
        (root / "10" / "cmdline").write_bytes(b"claude\xff\xfe\0\0--resume\0x\0")
        table = read_process_table(root)
        self.assertEqual(table[10].args, ("claude\ufffd\ufffd", "--resume", "x"))

    def test_processes_of_another_user_are_ignored(self):
        root = fake_proc(self.paths / "proc", {
            10: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=("claude",)),
        })
        self.assertEqual(sorted(read_process_table(root)), [10])
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(read_process_table(root), {})

    def test_an_unreadable_proc_root_yields_no_table(self):
        self.assertEqual(read_process_table(self.paths / "missing"), {})
        (self.paths / "afile").write_text("x", encoding="utf-8")
        self.assertEqual(read_process_table(self.paths / "afile"), {})
        without_btime = fake_proc(self.paths / "nobtime", {
            10: dict(ppid=1, start_ms=1_000_000, cwd="/w", args=("claude",)),
        }, btime_line=False)
        (without_btime / "stat").write_text("cpu  1 2 3 4\ncpu0 1 2 3\n", encoding="utf-8")
        self.assertEqual(read_process_table(without_btime), {})
        empty = fake_proc(self.paths / "empty", {}, btime_line=False)
        (empty / "stat").write_text("btime 1000\n", encoding="utf-8")
        self.assertEqual(read_process_table(empty), {})


# --- lineage: environment stamps ---------------------------------------------

class ParentStampTests(Hermetic):
    def environ(self, pid: int, entries: dict, folder: str = "proc") -> Path:
        return fake_proc(self.paths / folder, {pid: dict(env=entries)})

    def test_only_the_whitelisted_stamp_key_is_read(self):
        root = self.environ(5, {
            "PATH": "/usr/bin", "SECRET_TOKEN": "s3cr3t",
            "CLAUDE_CODE_SESSION_ID": "5906cf70-e7fd-4b86",
        })
        self.assertEqual(parent_stamp(5, root), "claude:5906cf70-e7fd-4b86")
        other = self.environ(6, {
            "CODEX_SESSION_ID": "abc", "CLAUDE_PARENT": "abc", "CLAUDE_CODE": "abc",
        }, folder="proc2")
        self.assertEqual(parent_stamp(6, other), "")
        tokens = self.environ(7, {"SECRET_TOKEN": "../../etc/passwd"}, folder="proc3")
        self.assertEqual(parent_stamp(7, tokens), "")

    def test_stamp_values_that_could_escape_or_flood_are_refused(self):
        cases = {
            "traversal": "../../../etc/passwd",
            "slash": "a/b",
            "colon": "abc:def",
            "space": "abc def",
            "dot": "..",
            "empty": "   ",
            "newline": "abc\ndef",
            "huge": "a" * 129,
        }
        for name, value in cases.items():
            with self.subTest(value=name):
                root = fake_proc(self.paths / f"proc-{name}", {
                    9: dict(env={"CLAUDE_CODE_SESSION_ID": value}),
                })
                self.assertEqual(parent_stamp(9, root), "")
        # The documented maximum length is still accepted, as is padding.
        longest = "a" * 128
        padded = fake_proc(self.paths / "proc-long", {
            9: dict(env={"CLAUDE_CODE_SESSION_ID": f"  {longest}  "}),
        })
        self.assertEqual(parent_stamp(9, padded), f"claude:{longest}")

    def test_a_bad_stamp_falls_through_to_the_next_known_one(self):
        root = self.environ(9, {"CLAUDE_CODE_SESSION_ID": "../escape", "OTHER": "x"},
                            folder="proc")
        self.assertEqual(parent_stamp(9, root), "")
        later = self.environ(9, {"CLAUDE_CODE_SESSION_ID": "", "OTHER": "x"}, folder="proc2")
        self.assertEqual(parent_stamp(9, later), "")

    def test_missing_or_unreadable_environments_and_pids_are_empty(self):
        root = fake_proc(self.paths / "proc", {5: dict(env={"CLAUDE_CODE_SESSION_ID": "abc"})})
        (root / "5" / "environ").unlink()
        self.assertEqual(parent_stamp(5, root), "")
        self.assertEqual(parent_stamp(6, root), "")
        self.assertEqual(parent_stamp(-1, root), "")
        self.assertEqual(parent_stamp(0, root), "")
        self.assertEqual(parent_stamp(2**31, root), "")


# --- lineage: recognising headless children ----------------------------------

class HeadlessChildTests(unittest.TestCase):
    def test_only_non_interactive_runs_count(self):
        cases = {
            ("opencode2", "run", "go"): "opencode",
            ("/opt/bin/opencode", "run"): "opencode",
            ("/opt/bin/opencode2", "run", "-m", "astra"): "opencode",
            ("claude", "-p", "hi"): "claude",
            ("claude", "--print", "hi"): "claude",
            ("codex", "exec", "--full-auto"): "codex",
            ("opencode2",): "",
            ("opencode2", "/w"): "",                      # a TUI, not headless
            ("opencode2", "serve"): "",
            ("opencode2", "client", "run"): "",
            ("opencode2-client", "run", "go"): "opencode",
            ("claude", "--resume", "abc"): "",
            ("claude", "chat", "hi"): "",
            ("codex", "resume", "abc"): "",
            ("codex", "-p", "hi"): "",
            ("codex", "exec"): "codex",
            ("/bin/sh", "-c", "claude -p hi"): "",
            ("python3", "claude"): "",
            ("node", "/x/claude/cli.js", "-p", "hi"): "",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.assertEqual(headless_child_harness(args), expected)


# --- lineage: observation ----------------------------------------------------

class ObserveLineageTests(Hermetic):
    def table(self, *processes: ProcessInfo) -> dict:
        return {item.pid: item for item in processes}

    def test_an_environment_stamp_beats_the_process_tree(self):
        child = session("ses_a", "/w/a", 10_000, harness="opencode")
        table = self.table(
            process(50, 1, 0, "/w", ("claude", "--resume", "tree")),
            process(60, 50, 9_000, "/w/a", ("/opt/x/opencode2", "run", "-m", "m", "go")),
        )
        found = observe_lineage(
            [child], {50: "claude:from-tree"}, table, stamp=lambda pid: "claude:from-stamp")
        self.assertEqual(found, {"ses_a": "claude:from-stamp"})

    def test_re_parented_orphans_are_still_attributed(self):
        orphan = session("ses_a", "/w/a", 10_000, harness="opencode")
        table = self.table(process(60, 1, 9_000, "/w/a", ("opencode2", "run")))
        self.assertEqual(
            observe_lineage([orphan], {}, table, stamp=lambda pid: "claude:orphan"),
            {"ses_a": "claude:orphan"},
        )
        # No stamp and a parent that is already gone: nothing to attribute.
        self.assertEqual(
            observe_lineage([orphan], {999: "claude:gone"}, table, stamp=lambda pid: ""), {})

    def test_the_nearest_known_ancestor_wins(self):
        child = session("ses_a", "/w/a", 10_000, harness="opencode")
        near = self.table(
            process(50, 1, 0, "/w", ("claude", "--resume", "a")),
            process(60, 50, 0, "/w", ("sh", "-c", "true")),
            process(70, 60, 0, "/w", ("claude", "--resume", "b")),
            process(80, 70, 9_000, "/w/a", ("opencode2", "run")),
        )
        self.assertEqual(
            observe_lineage([child], {50: "claude:a", 70: "claude:b"}, near, stamp=lambda pid: ""),
            {"ses_a": "claude:b"},
        )
        # With the nearer ancestor unknown, the walk keeps climbing.
        far = self.table(
            process(50, 1, 0, "/w", ("claude", "--resume", "a")),
            process(60, 50, 0, "/w", ("sh", "-c", "true")),
            process(80, 60, 9_000, "/w/a", ("opencode2", "run")),
        )
        self.assertEqual(
            observe_lineage([child], {50: "claude:a"}, far, stamp=lambda pid: ""),
            {"ses_a": "claude:a"},
        )

    def test_parent_cycles_in_the_process_tree_terminate(self):
        child = session("ses_a", "/w/a", 10_000, harness="opencode")
        table = self.table(
            process(70, 71, 9_000, "/w/a", ("opencode2", "run")),
            process(71, 70, 0, "/w/a", ("opencode2", "run")),
        )
        self.assertEqual(observe_lineage([child], {}, table, stamp=lambda pid: ""), {})
        self_loop = self.table(process(80, 80, 9_000, "/w/a", ("opencode2", "run")))
        self.assertEqual(observe_lineage([child], {}, self_loop, stamp=lambda pid: ""), {})

    def test_only_the_childs_own_harness_can_be_matched(self):
        # A codex child in /w: the claude and opencode sessions there are not its parents.
        table = self.table(
            process(60, 1, 9_000, "/w", ("codex", "exec", "go")),
            process(70, 1, 9_000, "/w2", ("claude", "-p", "go")),
        )
        sessions = [
            session("claude:p1", "/w", 10_000, harness="claude"),
            session("codex:s1", "/w", 10_000, harness="codex"),
            session("ses_open", "/w", 10_000, harness="opencode"),
            session("codex:s2", "/w2", 10_000, harness="codex"),
            session("ses_open2", "/w2", 10_000, harness="opencode"),
        ]
        self.assertEqual(
            observe_lineage(sessions, {}, table, stamp=lambda pid: "claude:parent"),
            {"codex:s1": "claude:parent"},
        )

    def test_sessions_that_already_have_a_parent_are_left_alone(self):
        real = session("ses_a", "/w/a", 10_000, harness="opencode", parent_id="ses_real")
        observed = session("ses_b", "/w/a", 10_000, harness="opencode", agent_parent_id="ses_other")
        plain = session("ses_c", "/w/a", 10_000, harness="opencode")
        table = self.table(process(60, 1, 9_000, "/w/a", ("opencode2", "run")))
        found = observe_lineage(
            [real, observed, plain], {}, table, stamp=lambda pid: "claude:parent")
        self.assertEqual(found, {"ses_c": "claude:parent"})

    def test_the_start_time_window_is_inclusive_at_both_ends(self):
        table = self.table(process(60, 1, 100_000, "/w/a", ("opencode2", "run")))
        for delta, linked in ((-5_000, True), (-5_001, False), (120_000, True),
                              (120_001, False), (0, True), (-5_002, False)):
            with self.subTest(delta=delta):
                candidate = session("ses_a", "/w/a", 100_000 + delta, harness="opencode")
                found = observe_lineage(
                    [candidate], {}, table, stamp=lambda pid: "claude:parent")
                self.assertEqual(found, {"ses_a": "claude:parent"} if linked else {})
        # A session with an unknown creation time is never inside the window.
        undated = session("ses_a", "/w/a", 0, harness="opencode")
        self.assertEqual(observe_lineage([undated], {}, table, stamp=lambda pid: "claude:p"), {})

    def test_the_nearest_session_in_the_window_is_the_parent(self):
        table = self.table(process(60, 1, 100_000, "/w/a", ("opencode2", "run")))
        sessions = [
            session("ses_far", "/w/a", 160_000, harness="opencode"),
            session("ses_near", "/w/a", 101_000, harness="opencode"),
            session("ses_just_before", "/w/a", 96_000, harness="opencode"),
        ]
        self.assertEqual(
            observe_lineage(sessions, {}, table, stamp=lambda pid: "claude:parent"),
            {"ses_near": "claude:parent"},
        )

    def test_a_session_is_never_its_own_parent(self):
        table = self.table(process(60, 1, 9_000, "/w", ("claude", "-p", "go")))
        self.assertEqual(
            observe_lineage([session("claude:self", "/w", 10_000, harness="claude")],
                            {}, table, stamp=lambda pid: "claude:self"),
            {},
        )

    def test_directories_are_normalised_before_matching(self):
        table = self.table(
            process(60, 1, 9_000, str(self.home / "proj"), ("opencode2", "run")),
            process(70, 1, 9_000, "/w/./b", ("codex", "exec")),
        )
        candidates = [
            session("ses_home", f"{self.home}/proj/", 10_000, harness="opencode"),
            session("ses_dot", "/w/b/", 10_000, harness="codex"),
            session("ses_never", "/w/b", 10_000, harness="opencode"),
        ]
        self.assertEqual(
            observe_lineage(candidates, {}, table, stamp=lambda pid: "claude:parent"),
            {"ses_home": "claude:parent", "ses_dot": "claude:parent"},
        )

    def test_sessions_of_other_directories_and_empty_snapshots_are_ignored(self):
        table = self.table(process(60, 1, 9_000, "/w/a", ("opencode2", "run")))
        self.assertEqual(observe_lineage([], {}, table, stamp=lambda pid: "claude:p"), {})
        self.assertEqual(
            observe_lineage([session("ses_a", "/w/other", 10_000)], {}, table,
                            stamp=lambda pid: "claude:p"),
            {},
        )

    def test_several_children_of_one_parent_are_all_nested(self):
        table = self.table(
            process(60, 1, 9_000, "/w/a", ("opencode2", "run")),
            process(70, 1, 9_100, "/w/b", ("opencode2", "run")),
            process(80, 1, 9_000, "/w/a", ("opencode2", "/w/a")),  # a TUI, not headless
        )
        sessions = [
            session("ses_a", "/w/a", 10_000, harness="opencode"),
            session("ses_b", "/w/b", 10_000, harness="opencode"),
        ]
        self.assertEqual(
            observe_lineage(sessions, {}, table, stamp=lambda pid: "claude:parent"),
            {"ses_a": "claude:parent", "ses_b": "claude:parent"},
        )

    def test_an_invalid_stamp_still_lets_the_process_tree_decide(self):
        child = session("ses_a", "/w/a", 10_000, harness="opencode")
        table = self.table(
            process(50, 1, 0, "/w", ("claude", "--resume", "p1")),
            process(60, 50, 9_000, "/w/a", ("opencode2", "run")),
        )
        self.assertEqual(
            observe_lineage([child], {50: "claude:from-tree"}, table, stamp=lambda pid: ""),
            {"ses_a": "claude:from-tree"},
        )


# --- lineage: applying and remembering ---------------------------------------

class ApplyLineageTests(Hermetic):
    def setUp(self):
        super().setUp()
        self.path = self.paths / "lineage.json"
        self.parent = session("claude:p1", "/w", 1_000, harness="claude")
        self.child = session("ses_a", "/w/a", 10_000, harness="opencode")
        self.table = {
            50: process(50, 1, 0, "/w", ("claude", "--resume", "p1")),
            60: process(60, 50, 9_000, "/w/a", ("opencode2", "run")),
        }
        self.stamps = {60: "claude:p1"}

    def apply(self, sessions, *, parents_by_pid=None, path="default", table="default"):
        """``apply_lineage`` with a fake process table and a temp lineage file."""
        return apply_lineage(
            snapshot(*sessions), parents_by_pid or {},
            self.path if path == "default" else path,
            self.table if table == "default" else table,
        )

    def stamp(self, pid):
        return self.stamps.get(pid, "")

    def test_an_observed_child_is_nested_and_remembered(self):
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, self.child])
        by_id = {item.id: item for item in result.sessions}
        self.assertEqual(by_id["ses_a"].agent_parent_id, "claude:p1")
        self.assertEqual(by_id["claude:p1"].agent_parent_id, "")
        self.assertEqual(load_lineage(self.path), {"ses_a": "claude:p1"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        # Remembered after the helper exits: no processes, no parents.
        later = apply_lineage(snapshot(self.parent, self.child), {}, self.path, {})
        self.assertEqual({item.id: item.agent_parent_id for item in later.sessions}["ses_a"],
                         "claude:p1")

    def test_unchanged_lineage_is_not_rewritten(self):
        calls = []
        original = harnesses._write_private_json

        def spy(target, payload):
            calls.append(Path(target))
            return original(target, payload)

        with mock.patch("ocdeck.harnesses._write_private_json", spy), \
                mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            self.apply([self.parent, self.child])
            self.assertEqual(len(calls), 1)
            self.apply([self.parent, self.child])
            self.assertEqual(len(calls), 1, "an unchanged lineage must not be rewritten")
        self.assertEqual(load_lineage(self.path), {"ses_a": "claude:p1"})

    def test_a_corrupt_or_foreign_lineage_file_is_survivable(self):
        snapshots = snapshot(self.parent, self.child)
        for payload in ("{not json", "[]", '"claude:p1"', "17", "null", ""):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                self.assertEqual(load_lineage(self.path), {})
                result = apply_lineage(snapshots, {}, self.path, {})
                self.assertEqual([s.agent_parent_id for s in result.sessions], ["", ""])
        # Values that are not strings (and keys that are not strings) are dropped.
        self.path.write_text(
            json.dumps({"ses_a": 5, "ses_b": "claude:p1", "ses_c": None}), encoding="utf-8",
        )
        self.assertEqual(load_lineage(self.path), {"ses_b": "claude:p1"})
        result = apply_lineage(
            snapshot(self.parent, self.child, session("ses_b", "/w/a", 10_000)),
            {}, self.path, {},
        )
        parents = {item.id: item.agent_parent_id for item in result.sessions}
        self.assertEqual(parents, {"claude:p1": "", "ses_a": "", "ses_b": "claude:p1"})

    def test_a_missing_lineage_file_is_created_with_its_parent_directory(self):
        target = self.paths / "deep" / "nested" / "lineage.json"
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            self.apply([self.parent, self.child], path=target)
        self.assertEqual(load_lineage(target), {"ses_a": "claude:p1"})
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_a_read_only_directory_never_breaks_the_snapshot(self):
        if os.getuid() == 0:
            self.skipTest("root ignores directory permissions")
        folder = self.paths / "read-only"
        folder.mkdir(mode=0o500)
        self.addCleanup(folder.chmod, 0o700)
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, self.child], path=folder / "lineage.json")
        by_id = {item.id: item for item in result.sessions}
        self.assertEqual(by_id["ses_a"].agent_parent_id, "claude:p1")
        self.assertFalse((folder / "lineage.json").exists())
        self.assertEqual(list(folder.iterdir()), [])

    def test_an_unwritable_path_never_breaks_the_snapshot(self):
        # A directory in place of the file: the atomic replace fails, the deck does not.
        self.path.mkdir()
        self.addCleanup(self.path.rmdir)
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, self.child])
        self.assertEqual({item.id: item.agent_parent_id for item in result.sessions}["ses_a"],
                         "claude:p1")

    def test_parents_missing_from_the_snapshot_are_never_linked(self):
        self.path.write_text(json.dumps({"ses_a": "claude:gone"}), encoding="utf-8")
        result = self.apply([self.child], table={})
        self.assertEqual(result.sessions[0].agent_parent_id, "")
        # A live observation is still remembered for a later snapshot that has the parent.
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            orphan = self.apply([self.child])
        self.assertEqual(orphan.sessions[0].agent_parent_id, "")
        self.assertEqual(load_lineage(self.path)["ses_a"], "claude:p1")
        later = self.apply([self.parent, self.child], table={})
        by_id = {item.id: item for item in later.sessions}
        self.assertEqual(by_id["ses_a"].agent_parent_id, "claude:p1")
        self.assertEqual(by_id["claude:p1"].agent_parent_id, "")

    def test_sessions_with_an_existing_parent_keep_it(self):
        own = session("ses_b", "/w/a", 10_000, harness="opencode", parent_id="ses_real")
        observed = session("ses_c", "/w/a", 10_000, harness="opencode", agent_parent_id="ses_old")
        self.path.write_text(
            json.dumps({"ses_a": "claude:p1", "ses_b": "claude:p1", "ses_c": "claude:p1"}),
            encoding="utf-8",
        )
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, own, observed, self.child], table={})
        parents = {item.id: item.agent_parent_id for item in result.sessions}
        self.assertEqual(parents, {
            "claude:p1": "", "ses_b": "", "ses_c": "ses_old", "ses_a": "claude:p1",
        })
        self.assertEqual(result.sessions[1].parent_id, "ses_real")

    def test_the_lineage_file_keeps_only_the_last_500_entries(self):
        self.path.write_text(
            json.dumps({f"old-{index}": f"claude:p{index}" for index in range(LINEAGE_LIMIT)}),
            encoding="utf-8",
        )
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, self.child])
        stored = load_lineage(self.path)
        self.assertEqual(len(stored), LINEAGE_LIMIT)
        self.assertIn("ses_a", stored)
        self.assertNotIn("old-0", stored)
        self.assertIn(f"old-{LINEAGE_LIMIT - 1}", stored)
        self.assertEqual({item.id: item.agent_parent_id for item in result.sessions}["ses_a"],
                         "claude:p1")
        # Trimming does not break later applications.
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            again = self.apply([self.parent, self.child])
        self.assertEqual(len(load_lineage(self.path)), LINEAGE_LIMIT)
        self.assertEqual({item.id: item.agent_parent_id for item in again.sessions}["ses_a"],
                         "claude:p1")

    def test_the_default_lineage_file_follows_the_state_directory(self):
        self.assertEqual(default_lineage_file(), self.state_home / "ocdeck" / "lineage.json")
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_STATE_HOME")
            self.assertEqual(default_lineage_file(), self.home / ".local/state/ocdeck/lineage.json")
        with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=self.stamp):
            result = self.apply([self.parent, self.child], path=None)
        self.assertEqual(load_lineage(default_lineage_file()), {"ses_a": "claude:p1"})
        self.assertEqual({item.id: item.agent_parent_id for item in result.sessions}["ses_a"],
                         "claude:p1")

    def test_a_snapshot_with_nothing_to_remember_is_returned_untouched(self):
        empty = snapshot(self.child)
        self.assertIs(apply_lineage(empty, {}, self.path, {}), empty)
        self.assertFalse(self.path.exists())


class LineageFromFakeProcTests(Hermetic):
    """read_process_table + parent_stamp + observe_lineage + apply_lineage, end to end."""

    def test_a_fake_proc_tree_nests_stamped_and_reparented_children(self):
        root = fake_proc(self.paths / "proc", {
            50: dict(ppid=1, start_ms=1_000_000, cwd="/w",
                     args=("claude", "--resume", "p1"), env={"TERM": "xterm"}),
            60: dict(ppid=50, start_ms=1_500_000, cwd="/w/a",
                     args=("/opt/x/opencode2", "run", "-m", "astra", "go"), env={"TERM": "xterm"}),
            70: dict(ppid=1, start_ms=1_500_000, cwd="/w/b", args=("codex", "exec", "go"),
                     env={"CLAUDE_CODE_SESSION_ID": "p9"}),
            80: dict(ppid=50, start_ms=1_500_000, cwd="/w/c", args=("opencode2", "/w/c"),
                     env={"TERM": "xterm"}),
        })
        table = read_process_table(root)
        self.assertEqual(sorted(table), [50, 60, 70, 80])
        sessions = [
            session("claude:p1", "/w", 1_000_000, harness="claude"),
            session("claude:p9", "/w", 1_000_000, harness="claude"),
            session("ses_a", "/w/a", 1_510_000, harness="opencode"),   # tree child of 50
            session("ses_b", "/w/b", 1_505_000, harness="codex"),       # stamped, ppid 1
            session("ses_c", "/w/c", 1_505_000, harness="opencode"),   # a TUI, not headless
        ]
        parents = {50: "claude:p1", 70: "claude:p9"}
        with mock.patch("ocdeck.harnesses.parent_stamp",
                        side_effect=lambda pid: parent_stamp(pid, root)):
            observed = observe_lineage(sessions, parents, table)
            self.assertEqual(observed, {"ses_a": "claude:p1", "ses_b": "claude:p9"})
            path = self.paths / "lineage.json"
            result = apply_lineage(snapshot(*sessions), parents, path, table)
        nesting = {item.id: item.agent_parent_id for item in result.sessions}
        self.assertEqual(nesting, {
            "claude:p1": "", "claude:p9": "", "ses_a": "claude:p1",
            "ses_b": "claude:p9", "ses_c": "",
        })
        self.assertEqual(load_lineage(path), {"ses_a": "claude:p1", "ses_b": "claude:p9"})


if __name__ == "__main__":
    unittest.main()
