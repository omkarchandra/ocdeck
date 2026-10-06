#!/usr/bin/env python3
"""Top-bar launcher for OC Deck."""

import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import Gtk


WORKSPACE = Path(__file__).resolve().parent.parent
OCDECK_COMMAND = (
    'exec "$HOME/.local/bin/ocdeck-entrypoint"'
)


class OCDeckIndicator:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            "ocdeck-launcher",
            "utilities-terminal-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("OC Deck")
        self.indicator.set_label("OC Deck", "OC Deck")

        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open OC Deck")
        open_item.connect("activate", self.open_ocdeck)
        menu.append(open_item)

        hint = Gtk.MenuItem(label="Middle-click the indicator for quick launch")
        hint.set_sensitive(False)
        menu.append(hint)

        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(open_item)

    def open_ocdeck(self, _item):
        subprocess.Popen(
            [
                "/usr/bin/ptyxis",
                "--standalone",
                "--tab",
                "--title",
                "OC Deck",
                f"--working-directory={WORKSPACE}",
                "--",
                "/usr/bin/bash",
                "-lc",
                OCDECK_COMMAND,
            ],
            start_new_session=True,
        )


if __name__ == "__main__":
    OCDeckIndicator()
    Gtk.main()
