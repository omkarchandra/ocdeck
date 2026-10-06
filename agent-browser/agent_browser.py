#!/usr/bin/env python3
"""Persistent agent-only Chrome, shared by MCP clients on a chosen monitor."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import websocket

from cdp_focus_guard import start_focus_guard


DEFAULT_CONFIG = Path.home() / ".config/opencode/agent-browser.json"
SERVICE = "opencode-agent-browser.service"
MONITOR_QUERY = """
import json
from gi.repository import Gio
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
state = bus.call_sync('org.gnome.Mutter.DisplayConfig',
    '/org/gnome/Mutter/DisplayConfig', 'org.gnome.Mutter.DisplayConfig',
    'GetCurrentState', None, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()
sizes = {}
for spec, modes, properties in state[1]:
    mode = next((m for m in modes if m[6].get('is-current')), None)
    if mode:
        sizes[spec[0]] = mode[1:3]
result = []
for x, y, scale, transform, primary, monitors, properties in state[2]:
    for spec in monitors:
        width, height = sizes[spec[0]]
        if transform in (1, 3, 5, 7):
            width, height = height, width
        result.append(dict(connector=spec[0], x=x, y=y, width=round(width / scale),
                           height=round(height / scale), primary=primary))
print(json.dumps(result))
"""


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get("enabled") is not True:
        raise ValueError("The dedicated agent browser is disabled")
    if type(config.get("port")) is not int or not 1024 <= config["port"] <= 65535:
        raise ValueError("Set a valid local debugging port in agent-browser.json")
    if not Path(config["profile"]).is_absolute() or not Path(config["browser"]).is_file():
        raise ValueError("The agent browser executable and absolute profile path are required")
    if "headless" in config and not isinstance(config["headless"], bool):
        raise ValueError("Set headless to true or false in agent-browser.json")
    return config


def monitors():
    result = subprocess.run(["/usr/bin/python3", "-c", MONITOR_QUERY],
                            capture_output=True, text=True, check=True, timeout=8)
    return json.loads(result.stdout)


def monitor_bounds(available, connector):
    monitor = next((item for item in available if item["connector"] == connector), None)
    if monitor is None:
        raise ValueError(f"Monitor {connector} is not active; choose one with oc_agent_browser place --monitor NAME")
    width, height = min(1600, monitor["width"] - 64), min(1050, monitor["height"] - 96)
    return {"left": monitor["x"] + 32, "top": monitor["y"] + 48, "width": width, "height": height}


def wayland_window(config):
    """Run the headed browser as a native Wayland window ("window_backend": "wayland")."""
    return config.get("window_backend") == "wayland" and not config.get("headless")


def focus_guard_enabled(config):
    """Opt-in only ("focus_guard": true). Off by default: suppressing
    bringToFront leaves the agent's tab in the background, where headed Chrome
    stops rendering frames, so Playwright's visible-and-stable check never
    passes and every click/hover times out (seen in practice)."""
    return config.get("focus_guard", False) is True and not config.get("headless")


def chrome_port(config):
    """The port Chrome itself listens on: private when the focus guard is on."""
    if not focus_guard_enabled(config):
        return config["port"]
    port = config.get("chrome_port", config["port"] + 1)
    if type(port) is not int or not 1024 <= port <= 65535 or port == config["port"]:
        raise ValueError("Set a valid chrome_port (not the agents' port) in agent-browser.json")
    return port


def endpoint(config):
    # The controller always talks to Chrome directly, so status, placement,
    # checkpoints and tab cleanup keep working even if the guard stopped.
    return f"http://127.0.0.1:{chrome_port(config)}"


def version(config):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(endpoint(config) + "/json/version", timeout=2) as response:
        return json.load(response)


def wait_ready(config, seconds=15, process=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("The dedicated Chrome process exited during startup")
        try:
            return version(config)
        except (OSError, ValueError):
            time.sleep(0.2)
    raise RuntimeError("The dedicated browser did not become ready; inspect opencode-agent-browser.service")


class CDP:
    def __init__(self, config):
        self.socket = websocket.create_connection(version(config)["webSocketDebuggerUrl"],
                                                   timeout=8, suppress_origin=True)
        self.counter = 0

    def call(self, method, params=None):
        self.counter += 1
        self.socket.send(json.dumps({"id": self.counter, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") != self.counter:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome rejected {method}: {message['error']['message']}")
            return message.get("result", {})

    def close(self):
        self.socket.close()


def window_ids(cdp):
    pages = [target for target in cdp.call("Target.getTargets")["targetInfos"] if target["type"] == "page"]
    return sorted({cdp.call("Browser.getWindowForTarget", {"targetId": page["targetId"]})["windowId"]
                   for page in pages})


def place(config, connector=None):
    selected = connector or config["monitor"]
    bounds = monitor_bounds(monitors(), selected)
    cdp = CDP(config)
    try:
        windows = window_ids(cdp)
        moved = []
        for window in windows:
            current = cdp.call("Browser.getWindowBounds", {"windowId": window}).get("bounds", {})
            if current.get("windowState") == "normal" and all(
                    current.get(key) == bounds[key] for key in ("left", "top", "width", "height")):
                continue  # Already where it belongs; never present or raise it again.
            cdp.call("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "normal"}})
            cdp.call("Browser.setWindowBounds", {"windowId": window, "bounds": bounds})
            moved.append(window)
        return {"monitor": selected, "requestedBounds": bounds, "windows": windows, "moved": moved}
    finally:
        cdp.close()


def status(config):
    cdp = CDP(config)
    try:
        pages = []
        for target in cdp.call("Target.getTargets")["targetInfos"]:
            if target["type"] != "page":
                continue
            title, url = target.get("title", ""), target.get("url", "")
            state = "unverified"
            if title.strip().lower() in {"just a moment...", "just a moment…", "attention required! | cloudflare"}:
                state = "human-verification-required"
            elif url.startswith("chrome-error://") or title.strip().lower() in {"403 forbidden", "access denied"}:
                state = "site-blocked"
            pages.append({"id": target["targetId"], "title": title, "url": url, "state": state})
        windows = [{"windowId": window, **cdp.call("Browser.getWindowBounds", {"windowId": window})}
                   for window in window_ids(cdp)]
        return {"profile": config["profile"], "endpoint": endpoint(config),
                "monitor": config["monitor"], "headless": config.get("headless", False),
                "connection": "ready", "needsAttention": any(page["state"] != "unverified" for page in pages),
                "pages": pages, "windows": windows}
    finally:
        cdp.close()


def save_tabs(config):
    """Checkpoint page destinations only; never capture or submit form contents."""
    cdp = CDP(config)
    try:
        urls = [target["url"] for target in cdp.call("Target.getTargets")["targetInfos"]
                if target["type"] == "page" and target.get("url", "").startswith(("https://", "http://"))]
        path = Path(config["profile"]) / "opencode-tabs.json"
        path.write_text(json.dumps({"version": 1, "urls": urls}, indent=2) + "\n")
        return urls
    finally:
        cdp.close()


def chrome_command(config, bounds):
    command = [config["browser"], f"--user-data-dir={config['profile']}",
               f"--remote-debugging-port={chrome_port(config)}", "--remote-debugging-address=127.0.0.1",
               "--class=opencode-agent-browser", "--no-first-run",
               "--no-default-browser-check", "--disable-session-crashed-bubble", "--disable-background-mode",
               "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
               # Bound memory: share processes across many tabs and keep disk caches small.
               "--renderer-process-limit=6", "--disk-cache-size=134217728", "--media-cache-size=67108864"]
    if config.get("headless"):
        command += ["--headless=new", "--disable-gpu"]
    else:
        if wayland_window(config):
            # GNOME never lets a native Wayland window take focus by itself, so
            # agents switching tabs can't steal the owner's keyboard focus.
            # Wayland clients can't position themselves; GNOME places it.
            # A covered Wayland window gets no frame callbacks, so the on-screen
            # tab nearly stops drawing and Playwright's "stable" check (which
            # needs frames) times out. Don't pace frames off the display.
            command += ["--ozone-platform=wayland", "--disable-gpu-vsync", "--disable-frame-rate-limit"]
        else:
            command += ["--ozone-platform=x11", "--window-position=%d,%d" % (bounds["left"], bounds["top"])]
    command += [f"--window-size={bounds['width']},{bounds['height']}"]
    # Supplying a new-window/start URL on every launch loses the working
    # conversation and other tabs. Let Chrome restore its own saved session.
    sessions = Path(config["profile"]) / "Default/Sessions"
    checkpoint = Path(config["profile"]) / "opencode-tabs.json"
    saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    urls = saved.get("urls", [])
    if not isinstance(urls, list) or not all(isinstance(url, str) and url.startswith(("http://", "https://")) for url in urls):
        raise ValueError("Invalid saved agent-browser tab destinations")
    if urls:
        # systemd/browser crashes can leave Chrome's native session incomplete.
        # An explicit URL checkpoint survives those unreliable restore cases.
        command += ["--new-window", *urls]
    elif any(sessions.glob("Session_*")):
        command.append("--restore-last-session")
    else:
        command += ["--new-window", config.get("start_url", "https://chatgpt.com/")]
    return command


def clean(config, keep=1):
    """Close agent tabs, keeping the most recent page targets."""
    cdp = CDP(config)
    try:
        pages = [target for target in cdp.call("Target.getTargets")["targetInfos"]
                 if target["type"] == "page"]
        closed = 0
        for page in pages[:max(0, len(pages) - keep)]:
            cdp.call("Target.closeTarget", {"targetId": page["targetId"]})
            closed += 1
        return {"closed": closed, "remaining": len(pages) - closed}
    finally:
        cdp.close()


DEFAULT_PURGE_IDLE_MINUTES = 5
PURGE_POLL_SECONDS = 30
DEFAULT_KEEP_TABS = ("https://calendar.google.com/",)


class TabJanitor:
    """Decide which agent tabs to close: each tab is judged on its own.

    A tab whose URL and title have not changed for ``idle_seconds`` is closed,
    unless its URL starts with a keep prefix (the owner's standing tabs). Every
    page is never closed at once, so Chrome never exits. Tracking
    is per tab, so busy agents in one tab never keep stale tabs elsewhere
    alive, and tabs restored after a restart are cleaned like any other.
    """

    def __init__(self, keep_prefixes, idle_seconds, now):
        self.keep = tuple(prefix for prefix in keep_prefixes if isinstance(prefix, str) and prefix)
        self.idle_seconds = idle_seconds
        self.seen = {}  # target -> ((url, title), unchanged since)
        self.started = now

    def observe(self, pages, now):
        seen = {}
        for target, url, title in pages:
            state = (url, title)
            previous = self.seen.get(target)
            since = previous[1] if previous and previous[0] == state else now
            seen[target] = (state, since)
        self.seen = seen
        if self.idle_seconds <= 0 or not seen:
            return []
        stale = [target for target, ((url, _title), since) in seen.items()
                 if now - since >= self.idle_seconds and not url.startswith(self.keep)]
        if len(stale) == len(seen):  # never close every page: keep the newest
            newest = max(stale, key=lambda target: seen[target][1])
            stale.remove(newest)
        return stale


def page_targets(cdp):
    return [(target["targetId"], target.get("url", ""), target.get("title", ""))
            for target in cdp.call("Target.getTargets")["targetInfos"] if target["type"] == "page"]


def purge_idle_tabs(config, janitor, now=None):
    """One janitor pass: close agent tabs if the browser has been idle."""
    cdp = CDP(config)
    try:
        closing = janitor.observe(page_targets(cdp), time.monotonic() if now is None else now)
        for target in closing:
            cdp.call("Target.closeTarget", {"targetId": target})
    finally:
        cdp.close()
    if closing:
        save_tabs(config)  # closed tabs must not come back on the next restart
    return closing


def start_tab_janitor(config):
    minutes = config.get("purge_idle_minutes", DEFAULT_PURGE_IDLE_MINUTES)
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return None
    keep = config.get("keep_tabs", list(DEFAULT_KEEP_TABS))
    janitor = TabJanitor(keep if isinstance(keep, list) else [], minutes * 60, time.monotonic())

    def loop():
        while True:
            time.sleep(PURGE_POLL_SECONDS)
            try:
                closed = purge_idle_tabs(config, janitor)
                if closed:
                    print(f"Closed {len(closed)} idle agent tab(s)", flush=True)
            except (OSError, ValueError, RuntimeError, websocket.WebSocketException) as error:
                print(f"Agent tab cleanup skipped: {error}", file=sys.stderr, flush=True)

    threading.Thread(target=loop, name="tab-janitor", daemon=True).start()
    return janitor


def serve(config):
    # Headless never needs the display server at all.
    bounds = ({"left": 0, "top": 0, "width": 1600, "height": 1050}
              if config.get("headless") else monitor_bounds(monitors(), config["monitor"]))
    for port in {config["port"], chrome_port(config)}:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    # The dedicated profile is separate from both normal Chrome and the old MCP profile.
    Path(config["profile"]).mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(chrome_command(config, bounds))
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        try:
            save_tabs(config)
        except (OSError, ValueError, RuntimeError, websocket.WebSocketException) as error:
            print(f"Agent browser tab checkpoint failed: {error}", file=sys.stderr, flush=True)
        process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        wait_ready(config, process=process)
        if not config.get("headless") and not wayland_window(config):
            place(config)
        if focus_guard_enabled(config):
            start_focus_guard(config["port"], chrome_port(config))
        print(json.dumps(status(config)), flush=True)
        try:
            start_tab_janitor(config)
        except (OSError, ValueError, RuntimeError, websocket.WebSocketException) as error:
            print(f"Agent tab cleanup unavailable: {error}", file=sys.stderr, flush=True)
        result = process.wait()
        return 0 if stopping else result
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def start(config):
    subprocess.run(["systemctl", "--user", "start", SERVICE], check=True, timeout=20)
    wait_ready(config)


def set_mode(config, path, headless):
    if config.get("headless", False) == headless:
        try:
            current = version(config)
            if ("HeadlessChrome/" in current.get("User-Agent", "")) == headless:
                return {"headless": headless, "monitor": config["monitor"], "restarted": False}
        except (OSError, ValueError):
            pass
    if not headless:
        # Validate the display before changing the saved mode or stopping Chrome.
        monitor_bounds(monitors(), config["monitor"])
    try:
        version(config)
    except (OSError, ValueError):
        pass  # An offline browser may still have a valid saved checkpoint.
    else:
        save_tabs(config)
    config["headless"] = headless
    path.write_text(json.dumps(config, indent=2) + "\n")
    subprocess.run(["systemctl", "--user", "restart", SERVICE], check=True, timeout=30)
    wait_ready(config, seconds=30)
    return {"headless": headless, "monitor": config["monitor"], "restarted": True}


def recover(config, path):
    """Expose a blocked site for normal user interaction, without retrying a task."""
    change = set_mode(config, path, False)
    result = status(config)
    result["restarted"] = change["restarted"]
    result["nextStep"] = (
        "Complete the site's verification in the dedicated browser on " + config["monitor"] +
        "; then inspect the original conversation before resuming the pending message."
        if result["needsAttention"] else
        "Inspect the original conversation and its composer before resuming. CDP connectivity alone does not prove a site is signed in or a message was delivered."
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?",
                         choices=("start", "serve", "status", "doctor", "recover", "place", "clean", "mode", "mcp"), default="start")
    parser.add_argument("argument", nargs="?", help="headed|headless for mode; optional integer for clean")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--monitor", help="save and use a monitor connector for place, e.g. eDP-1 or DP-1")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.monitor:
            if args.operation != "place":
                parser.error("--monitor is used with place")
            monitor_bounds(monitors(), args.monitor)
            config["monitor"] = args.monitor
            args.config.write_text(json.dumps(config, indent=2) + "\n")
        if args.operation == "serve":
            return serve(config)
        if args.operation == "mode":
            if args.argument not in {"headed", "headless"}:
                parser.error("mode requires headed or headless")
            print(json.dumps(set_mode(config, args.config, args.argument == "headless"), indent=2))
            return 0
        if args.operation == "recover":
            print(json.dumps(recover(config, args.config), indent=2))
            return 0
        if args.operation == "doctor":
            report = status(config)
            print(json.dumps(report, indent=2))
            return 2 if report["needsAttention"] else 0
        if args.operation in {"start", "mcp", "clean"}:
            start(config)
        if args.operation == "mcp":
            command = ["/usr/bin/npx", "-y", "@playwright/mcp@0.0.79", "--cdp-endpoint", endpoint(config),
                       "--allow-unrestricted-file-access", "--caps", "devtools,pdf",
                       "--image-responses", "omit", "--codegen", "none",
                       # Snappier action loops, more headroom for slow SPAs.
                       "--timeout-settle", "300", "--timeout-action", "10000"]
            os.execvpe(command[0], command, os.environ)
        if args.operation == "place":
            if config.get("headless"):
                print(json.dumps({"headless": True, "moved": False}, indent=2))
                return 0
            print(json.dumps(place(config), indent=2))
        if args.operation == "clean":
            print(json.dumps(clean(config, int(args.argument) if args.argument else 1), indent=2))
            return 0
        print(json.dumps(status(config), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, websocket.WebSocketException) as error:
        print(f"Agent browser: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
