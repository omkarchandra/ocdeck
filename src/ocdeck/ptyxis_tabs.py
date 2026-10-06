"""Select a named Ptyxis tab through GTK's native accessibility actions.

GTK4 does not implement AT-SPI GrabFocus, and Ptyxis does not expose tab UUIDs.
Its window does expose page.next/page.previous actions. Use those on the exact
application, then check its title; no global keys, pointer clicks, or clipboard.
"""
from collections import deque
import time


def accessible_nodes(root, limit=2000):
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < limit:
        node, depth = queue.popleft()
        if node is None or depth > 40:
            continue
        visited += 1
        yield node
        try:
            for index in range(node.get_child_count()):
                try:
                    queue.append((node.get_child_at_index(index), depth + 1))
                except Exception:
                    continue
        except Exception:
            continue


def invoke_action(node, name):
    try:
        action = node.get_action_iface()
        if action is not None:
            for index in range(action.get_n_actions()):
                if action.get_action_name(index) == name:
                    return bool(action.do_action(index))
    except Exception:
        pass
    return False


def select_tab(frame, title, *, settle=lambda: time.sleep(.05)):
    """Select within a frame that actually contains the named tab.

    A complete turn is bounded by the tab count and restores the initial tab
    if the requested title disappears. Re-read after every asynchronous action.
    """
    tabs = []
    for node in accessible_nodes(frame):
        try:
            if node.get_role_name() == "page tab":
                tabs.append(node.get_name())
        except Exception:
            continue
    if title not in tabs:
        # A single-tab Ptyxis window has no tab bar.
        return frame.get_name() == title
    if tabs.count(title) != 1:
        return False

    # Close an open tab overview through its own action before selecting.
    for node in accessible_nodes(frame):
        if invoke_action(node, "overview.close"):
            settle()
            break
    for _ in range(len(tabs)):
        frame.clear_cache()
        if frame.get_name() == title:
            return True
        if not invoke_action(frame, "page.next"):
            return False
        settle()
    frame.clear_cache()
    return frame.get_name() == title


def select_dashboard_tab(host_pid):
    # Loaded only by the system-Python helper (the dashboard venv needs no GI).
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    Atspi.init()
    Atspi.set_timeout(750, 750)

    def settle():
        until = time.monotonic() + .05
        context = GLib.MainContext.default()
        while time.monotonic() < until:
            while context.pending():
                context.iteration(False)
            time.sleep(.005)

    try:
        desktop = Atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            if app.get_process_id() != host_pid:
                continue
            for child in range(app.get_child_count()):
                frame = app.get_child_at_index(child)
                if frame.get_role() == Atspi.Role.FRAME and select_tab(frame, "OC Deck", settle=settle):
                    return True
    except Exception:
        return False
    finally:
        Atspi.exit()
    return False
