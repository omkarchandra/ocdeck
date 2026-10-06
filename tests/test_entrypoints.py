import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "bin/ocdeck-entrypoint"
SELECTOR = ROOT / "bin/ocdeck-backend"


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o700)
    return path


def backend_state(config: Path, value: str) -> Path:
    directory = config / "ocdeck"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    lock = directory / ".backend.lock"
    lock.write_text("")
    lock.chmod(0o600)
    selector = directory / "backend"
    selector.write_text(f"{value}\n")
    selector.chmod(0o600)
    return selector


def test_installer_provisions_managed_entrypoints():
    installer = (ROOT / "install.sh").read_text()
    for target in (
        "ocdeck-permission-watcher",
        "ocdeck-entrypoint",
        "ocdeck-backend",
    ):
        assert f'"$HOME/.local/bin/{target}"' in installer
    # Super+O is installed as a verified snapshot, not a link into the checkout.
    assert '"$ROOT/bin/install-ocdeck-hotkey"' in installer
    assert '"$ROOT/ocdeck_hotkey.py" "$HOME/.local/bin/ocdeck-hotkey"' not in installer
    assert "ln -sfn" not in installer
    assert 'install -m 0644 "$ROOT/ocdeck-hotkey.service"' in installer
    assert 'cp -R "$source_dir/." "$target_dir/"' in installer
    assert '"$ROOT/desktop/install-ocdeck-shortcuts.py"' in installer
    assert 'BACKEND="${OCDECK_INSTALL_BACKEND:-v2}"' in installer
    assert 'systemctl --user cat "$SERVICE"' in installer
    assert '"$HOME/.local/bin/ocdeck-backend" "$BACKEND"' in installer
    assert installer.index('"$HOME/.local/bin/ocdeck-backend" "$BACKEND"') < installer.index(
        "systemctl --user enable ocdeck-permission-watcher.service"
    )
    assert "systemctl --user enable ocdeck-hotkey.service" not in installer
    assert "--now" not in installer


def test_hotkey_has_one_canonical_accelerator_owner():
    unit = (ROOT / "ocdeck-hotkey.service").read_text()
    schema = (
        ROOT
        / "desktop/gnome-extensions/ocdeck-switch@local/schemas/"
        "org.gnome.shell.extensions.ocdeck-switch.gschema.xml"
    ).read_text()
    assert "ExecStart=%h/.local/bin/ocdeck-hotkey" in unit
    assert "Type=oneshot" in unit
    assert "Restart=always" not in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "<default>[]</default>" in schema


def test_managed_watcher_uses_shared_selector_without_unconditional_v2_dependency():
    unit = (ROOT / "systemd/ocdeck-permission-watcher.service").read_text()
    assert "Type=exec" in unit
    assert "ExecStart=%h/.local/bin/ocdeck-entrypoint" in unit
    assert "Environment=OCDECK_BIN=%h/.local/bin/ocdeck-permission-watcher" in unit
    assert "Requires=opencode2.service" not in unit


def test_entrypoint_requires_gated_v2_service_and_scrubs_credentials(tmp_path):
    calls = tmp_path / "calls"
    config = tmp_path / "config"
    backend_state(config, "v2")
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "systemctl:%s\\n" "$*" >>"$CALLS"\n',
    )
    ocdeck = executable(
        tmp_path / "ocdeck",
        'printf "ocdeck:%s:%s:%s:%s:%s\\n" "$*" '
        '"${OPENCODE_URL-unset}" "${OPENCODE_SERVER_USERNAME-unset}" '
        '"${OPENCODE_SERVER_PASSWORD-unset}" '
        '"${HOME_AGENT_GUARD_KEY_FILE-unset}" >>"$CALLS"\n',
    )
    env = {
        **os.environ,
        "CALLS": str(calls),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config),
        "OCDECK_BIN": str(ocdeck),
        "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        "OPENCODE_URL": "http://v1.invalid",
        "OPENCODE_SERVER_USERNAME": "legacy",
        "OPENCODE_SERVER_PASSWORD": "legacy-secret",
        "HOME_AGENT_GUARD_KEY_FILE": "/secret/guard.key",
    }

    result = subprocess.run(
        [ENTRYPOINT, "--once"], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "systemctl:--user cat opencode2-ready.service",
        "systemctl:--user start opencode2-ready.service",
        "ocdeck:--backend v2 --once:unset:unset:unset:unset",
    ]


def test_entrypoint_uses_persistent_v1_without_starting_v2(tmp_path):
    calls = tmp_path / "calls"
    config = tmp_path / "config"
    backend_state(config, "v1")
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "systemctl:%s\\n" "$*" >>"$CALLS"\n',
    )
    ocdeck = executable(
        tmp_path / "ocdeck",
        'printf "ocdeck:%s\\n" "$*" >>"$CALLS"\n',
    )

    result = subprocess.run(
        [ENTRYPOINT, "--once"],
        env={
            **os.environ,
            "CALLS": str(calls),
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(config),
            "OCDECK_BIN": str(ocdeck),
            "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "systemctl:--user cat opencode-web.service",
        "systemctl:--user start opencode-web.service",
        "ocdeck:--backend v1 --once",
    ]


def test_inherited_opencode_profile_uses_desktop_catalog_and_selector(tmp_path):
    config = tmp_path / ".config"
    private = config / "ocdeck-v2-runtime"
    backend_state(config, "v1")
    calls = tmp_path / "calls"
    systemctl = executable(tmp_path / "systemctl", 'printf "%s\\n" "$*" >>"$CALLS"\n')
    ocdeck = executable(tmp_path / "ocdeck", 'printf "%s\\n" "$XDG_CONFIG_HOME" >>"$CALLS"\n')
    environment = {
        **os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(private),
        "CALLS": str(calls), "OCDECK_BIN": str(ocdeck), "OCDECK_SYSTEMCTL_BIN": str(systemctl),
    }
    environment.pop("OCDECK_BACKEND_FILE", None)
    result = subprocess.run([ENTRYPOINT, "--once"], env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["--user cat opencode-web.service", "--user start opencode-web.service", str(config)]
    result = subprocess.run([
        sys.executable, "-c",
        "from ocdeck.source import DEFAULT_PROJECTS_FILE, DEFAULT_PROJECT_REGISTRY_FILE; "
        "print(DEFAULT_PROJECTS_FILE); print(DEFAULT_PROJECT_REGISTRY_FILE)",
    ], env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(config / "home-agent/projects.md"), str(config / "home-agent/registry.json")]


def test_selector_is_atomic_owner_only_and_restarts_active_services(tmp_path):
    calls = tmp_path / "calls"
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "%s\\n" "$*" >>"$CALLS"\n'
        'if [[ "$*" == "--user is-active --quiet ocdeck-permission-watcher.service" ]]; then exit 0; fi\n'
        'if [[ "$1" == "--user" && "$2" == "restart" ]]; then exit 0; fi\n'
        'exit 3\n',
    )
    directory = tmp_path / "selector"

    result = subprocess.run(
        [SELECTOR, "v1"],
        env={
            **os.environ,
            "CALLS": str(calls),
            "OCDECK_BACKEND_DIR": str(directory),
            "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    selector = directory / "backend"
    assert selector.read_text() == "v1\n"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(selector.stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / ".backend.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / ".backend.writer.lock").stat().st_mode) == 0o600
    assert calls.read_text().splitlines() == [
        "--user is-active --quiet ocdeck-permission-watcher.service",
        "--user restart ocdeck-permission-watcher.service",
    ]


def test_selector_from_managed_profile_updates_only_desktop_state(tmp_path):
    config = tmp_path / ".config"
    private = config / "ocdeck-v2-runtime"
    selector = backend_state(config, "v1")
    systemctl = executable(tmp_path / "systemctl", "exit 3\n")
    environment = {
        **os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(private),
        "OCDECK_SYSTEMCTL_BIN": str(systemctl),
    }
    environment.pop("OCDECK_BACKEND_DIR", None)
    result = subprocess.run([SELECTOR, "v2"], env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert selector.read_text() == "v2\n"
    assert not (private / "ocdeck/backend").exists()


def test_selector_attempts_every_active_restart_before_reporting_failure(tmp_path):
    calls = tmp_path / "calls"
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "%s\\n" "$*" >>"$CALLS"\n'
        'if [[ "$2" == "is-active" ]]; then exit 0; fi\n'
        'if [[ "$*" == "--user restart ocdeck-permission-watcher.service" ]]; then exit 1; fi\n'
        'exit 0\n',
    )
    directory = tmp_path / "selector"

    result = subprocess.run(
        [SELECTOR, "v1"],
        env={
            **os.environ,
            "CALLS": str(calls),
            "OCDECK_BACKEND_DIR": str(directory),
            "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert (directory / "backend").read_text() == "v1\n"
    assert calls.read_text().splitlines() == [
        "--user is-active --quiet ocdeck-permission-watcher.service",
        "--user restart ocdeck-permission-watcher.service",
        "--user stop ocdeck-permission-watcher.service",
    ]
    assert "ocdeck-permission-watcher.service" in result.stderr


def test_entrypoint_rejects_corrupt_selector(tmp_path):
    config = tmp_path / "config"
    backend_state(config, "xx")
    result = subprocess.run(
        [ENTRYPOINT],
        env={
            **os.environ,
            "XDG_CONFIG_HOME": str(config),
            "OCDECK_BIN": "/bin/true",
            "OCDECK_SYSTEMCTL_BIN": "/bin/true",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "exactly 'v1' or 'v2'" in result.stderr


def test_entrypoint_rejects_missing_and_permissive_selector_state(tmp_path):
    config = tmp_path / "config"
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config),
        "OCDECK_BIN": "/bin/true",
        "OCDECK_SYSTEMCTL_BIN": "/bin/true",
    }
    missing = subprocess.run(
        [ENTRYPOINT],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 2

    selector = backend_state(config, "v2")
    selector.chmod(0o644)
    permissive = subprocess.run(
        [ENTRYPOINT],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert permissive.returncode == 2
    assert "owner-only regular file" in permissive.stderr

    real = config / "real-backend"
    real.write_text("v2\n")
    real.chmod(0o600)
    selector.unlink()
    selector.symlink_to(real)
    symlinked = subprocess.run(
        [ENTRYPOINT],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert symlinked.returncode == 2
    assert "cannot be read safely" in symlinked.stderr


def test_selector_rejects_symlinked_directory_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "selector"
    link.symlink_to(target, target_is_directory=True)

    result = subprocess.run(
        [SELECTOR, "v2"],
        env={
            **os.environ,
            "OCDECK_BACKEND_DIR": str(link),
            "OCDECK_SYSTEMCTL_BIN": "/bin/true",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert not (target / "backend").exists()


def test_selector_does_not_commit_when_service_status_is_unavailable(tmp_path):
    directory = tmp_path / "config/ocdeck"
    backend_state(directory.parent, "v2")
    calls = tmp_path / "calls"
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "%s\\n" "$*" >>"$CALLS"\n'
        'if [[ "$*" == "--user is-active --quiet ocdeck-permission-watcher.service" ]]; then exit 1; fi\n'
        'exit 3\n',
    )

    result = subprocess.run(
        [SELECTOR, "v1"],
        env={
            **os.environ,
            "CALLS": str(calls),
            "OCDECK_BACKEND_DIR": str(directory),
            "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert (directory / "backend").read_text() == "v2\n"
    assert "Could not query" in result.stderr


def test_selector_restores_previous_value_after_post_replace_fsync_failure(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "config/ocdeck"
    backend_state(directory.parent, "v2")
    real_fsync = os.fsync
    calls = 0

    def fail_after_replace(descriptor):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("selector directory fsync failed")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_replace)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda arguments, check: subprocess.CompletedProcess(arguments, 3),
    )
    monkeypatch.setenv("OCDECK_BACKEND_DIR", str(directory))
    monkeypatch.setenv("OCDECK_SYSTEMCTL_BIN", "/bin/true")
    monkeypatch.setattr(sys, "argv", [str(SELECTOR), "v1"])

    with pytest.raises(OSError, match="selector directory fsync failed"):
        runpy.run_path(str(SELECTOR), run_name="__main__")

    assert (directory / "backend").read_text() == "v2\n"
    assert list(directory.glob(".backend.*.rollback")) == []


@pytest.mark.parametrize("arguments", [["--backend", "v1"], ["--ba=v1"], ["--b", "v1"]])
def test_entrypoint_rejects_caller_backend_override_before_service_start(
    tmp_path,
    arguments,
):
    calls = tmp_path / "calls"
    systemctl = executable(
        tmp_path / "systemctl",
        'printf "%s\\n" "$*" >>"$CALLS"\n',
    )

    result = subprocess.run(
        [ENTRYPOINT, *arguments],
        env={
            **os.environ,
            "CALLS": str(calls),
            "OCDECK_SYSTEMCTL_BIN": str(systemctl),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "do not allow a backend override" in result.stderr
    assert not calls.exists()
