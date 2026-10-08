from __future__ import annotations

import pytest
from unittest.mock import Mock


@pytest.fixture(autouse=True)
def _isolated_ocdeck_state(tmp_path, monkeypatch):
    """Keep default state paths (recent-open store and friends) out of $HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Launch commands in app tests pin the plain Claude flags; the permission hook
    # (--settings) has its own tests, which switch it back on.
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "0")
    # Fixture sessions must never restyle the owner's real tmux terminals.
    # Header integration tests opt into the real helper with a fake runner.
    monkeypatch.setattr("ocdeck.app.apply_header", Mock(return_value=True))
