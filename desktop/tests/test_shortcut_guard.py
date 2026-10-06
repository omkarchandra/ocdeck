import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("shortcut_guard", Path(__file__).parents[1] / "shortcut_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class Settings:
    def __init__(self, **values):
        self.values = values
        self.writes = 0

    def get(self, key):
        value = self.values.get(key, "")
        return list(value) if isinstance(value, list) else value

    def set(self, key, value):
        self.writes += 1
        self.values[key] = value

    get_string = get_strv = get
    set_string = set_strv = set


class ShortcutGuardTests(unittest.TestCase):
    def test_recovers_overwritten_list_and_preserves_new_shortcuts(self):
        settings = Settings(**{"custom-keybindings": ["/laptop-panel/", "/other/"]})
        shortcut = Settings()
        guard.reconcile(settings, shortcut, Path("/home/example"))
        self.assertEqual(settings.get_strv("custom-keybindings"), ["/laptop-panel/", "/other/", guard.PATH])
        self.assertEqual(shortcut.get_string("command"), "/home/example/.local/bin/ocdeck-hotkey --dispatch")
        settings.set_strv("custom-keybindings", ["/new-shortcut/"])
        guard.reconcile(settings, shortcut, Path("/home/example"))
        self.assertEqual(settings.get_strv("custom-keybindings"), ["/new-shortcut/", guard.PATH])

    def test_own_change_notifications_do_not_create_a_write_loop(self):
        settings = Settings(**{"custom-keybindings": []})
        shortcut = Settings()
        guard.reconcile(settings, shortcut, Path("/home/example"))
        writes = settings.writes + shortcut.writes
        for _ in range(100):
            self.assertEqual(guard.reconcile(settings, shortcut, Path("/home/example")), [])
        self.assertEqual(settings.writes + shortcut.writes, writes)

    def test_repairs_binding_without_reordering_the_desktop_list(self):
        settings = Settings(**{"custom-keybindings": ["/first/", guard.PATH, "/last/"]})
        shortcut = Settings()
        guard.reconcile(settings, shortcut, Path("/home/example"))
        shortcut.set_string("binding", "")
        self.assertEqual(guard.reconcile(settings, shortcut, Path("/home/example")), ["binding"])
        self.assertEqual(settings.writes, 0)


if __name__ == "__main__":
    unittest.main()
