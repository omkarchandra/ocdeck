#!/usr/bin/env python3
"""Focus the existing OC Deck window, or launch one if none is open."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocdeck_hotkey import focus_or_launch  # noqa: E402

if __name__ == "__main__":
    focus_or_launch()
