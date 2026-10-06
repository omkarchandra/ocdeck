import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const PTYXIS_WM_CLASS = /(^|\.)ptyxis$/i;
const OPENCODE_TITLE_PREFIX = 'OpenCode \u00b7 ';
const MAINTENANCE_TITLE = /mainten/i;
const AGENT_TILE_FRACTION = 0.25;
// Your pinned viewer slot (Shift+P in OC Deck): every new session window
// opens at exactly this position and size. Kept across logins.
const SLOT_FILE = GLib.build_filenamev([GLib.get_user_state_dir(), 'ocdeck', 'viewer-slot.json']);
// Where the OC Deck window itself opens (PinDeck), kept across logins.
const DECK_SLOT_FILE = GLib.build_filenamev([GLib.get_user_state_dir(), 'ocdeck', 'deck-slot.json']);
const DECK_TITLE = 'OC Deck';
// Optional: keep the agents' dedicated Chrome on one monitor. As a native
// Wayland window it cannot place itself, so GNOME does it here. Off unless
// ~/.config/ocdeck/placement.json names a monitor: {"agentBrowserMonitor": "eDP-1"}
// (find connector names with `gnome-randr` or the Displays settings).
const PLACEMENT_FILE = GLib.build_filenamev([GLib.get_user_config_dir(), 'ocdeck', 'placement.json']);
const AGENT_BROWSER_CLASS = /opencode-agent-browser/i;
const AGENT_BROWSER_PROFILE = '/agent-browser-profile';
const AGENT_BROWSER_SETTLE_MS = 2000;   // after monitors change, let them settle
const DBUS_NAME = 'org.local.OCDeckPlacement';
const DBUS_PATH = '/org/local/OCDeckPlacement';
const DBUS_XML = `
<node>
  <interface name="${DBUS_NAME}">
    <method name="AgentSlot">
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
      <arg type="i" name="width" direction="out"/>
      <arg type="i" name="height" direction="out"/>
    </method>
    <method name="SetAgentReference">
      <arg type="s" name="session_name" direction="in"/>
      <arg type="b" name="stored" direction="out"/>
    </method>
    <method name="PlaceViewer">
      <arg type="s" name="session_name" direction="in"/>
      <arg type="b" name="placed" direction="out"/>
    </method>
    <method name="PinDeck">
      <arg type="b" name="pinned" direction="out"/>
    </method>
    <method name="FocusPid">
      <arg type="i" name="process_pid" direction="in"/>
      <arg type="b" name="focused" direction="out"/>
    </method>
  </interface>
</node>`;

export default class OCDeckPlacementExtension extends Extension {
    enable() {
        this._referenceWindow = null;
        this._referenceRect = null;
        this._referenceArea = null;
        this._windowCreatedId = 0;
        this._focusWindowId = 0;
        this._enteredMonitorId = 0;
        this._monitorsChangedId = 0;
        this._agentBrowserTimerId = 0;
        this._slot = this._loadSlot();

        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(DBUS_XML, this);
        this._dbusObject.export(Gio.DBus.session, DBUS_PATH);
        this._dbusOwnerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session,
            DBUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null);

        this._windowCreatedId = global.display.connect(
            'window-created', (_display, window) => this._onWindowCreated(window));
        this._focusWindowId = global.display.connect(
            'notify::focus-window', () => this._onFocusChanged());
        this._enteredMonitorId = global.display.connect(
            'window-entered-monitor', (_display, _monitor, window) => this._queueAgentBrowserCheck(window, 0));
        this._monitorsChangedId = Main.layoutManager.connect(
            'monitors-changed', () => this._queueAgentBrowserCheck(null, AGENT_BROWSER_SETTLE_MS));
        this._seedReferenceWindow();
        this._keepAgentBrowsersOnLaptop();
    }

    disable() {
        if (this._windowCreatedId) {
            global.display.disconnect(this._windowCreatedId);
            this._windowCreatedId = 0;
        }
        if (this._focusWindowId) {
            global.display.disconnect(this._focusWindowId);
            this._focusWindowId = 0;
        }
        if (this._enteredMonitorId) {
            global.display.disconnect(this._enteredMonitorId);
            this._enteredMonitorId = 0;
        }
        if (this._monitorsChangedId) {
            Main.layoutManager.disconnect(this._monitorsChangedId);
            this._monitorsChangedId = 0;
        }
        if (this._agentBrowserTimerId) {
            GLib.source_remove(this._agentBrowserTimerId);
            this._agentBrowserTimerId = 0;
        }
        this._referenceWindow = null;
        this._referenceRect = null;
        this._referenceArea = null;
        if (this._dbusOwnerId) {
            Gio.bus_unown_name(this._dbusOwnerId);
            this._dbusOwnerId = 0;
        }
        this._dbusObject?.unexport();
        this._dbusObject = null;
    }

    _ptyxisWindows() {
        return global.display
            .get_tab_list(Meta.TabList.NORMAL_ALL, null)
            .filter(window => PTYXIS_WM_CLASS.test(window.get_wm_class() ?? ''));
    }

    _windowProcessArguments(window) {
        const pid = window.get_pid();
        if (!pid)
            return [];

        try {
            const file = Gio.File.new_for_path(`/proc/${pid}/cmdline`);
            const [loaded, contents] = file.load_contents(null);
            if (!loaded)
                return [];
            return new TextDecoder()
                .decode(contents)
                .split('\0')
                .filter(Boolean);
        } catch (_error) {
            return [];
        }
    }

    _tmuxWindow(sessionName) {
        return this._ptyxisWindows().find(window => {
            if ((window.get_title() ?? '').includes(sessionName))
                return true;
            const args = this._windowProcessArguments(window);
            // Attaches use tmux's exact-match target "=name"; older windows use "name".
            return args.includes('attach-session')
                && (args.includes(sessionName) || args.includes(`=${sessionName}`));
        });
    }

    _isOpenCodeWindow(window) {
        return PTYXIS_WM_CLASS.test(window.get_wm_class() ?? '') &&
            (window.get_title() ?? '').startsWith(OPENCODE_TITLE_PREFIX);
    }

    _referenceWindowValid() {
        return Boolean(this._referenceWindow &&
            this._referenceWindow.get_compositor_private());
    }

    _rememberReferenceWindow(window) {
        if (!window || !this._isOpenCodeWindow(window))
            return false;
        const frame = window.get_frame_rect();
        this._referenceWindow = window;
        this._referenceRect = {
            x: frame.x,
            y: frame.y,
            width: frame.width,
            height: frame.height,
        };
        this._referenceArea = window.get_work_area_current_monitor();
        return true;
    }

    _opencodeWindows() {
        return this._ptyxisWindows().filter(window =>
            this._isOpenCodeWindow(window));
    }

    _loadSlot() {
        try {
            const [ok, bytes] = GLib.file_get_contents(SLOT_FILE);
            if (!ok)
                return null;
            return this._validSlot(JSON.parse(new TextDecoder().decode(bytes)));
        } catch (_error) {
            return null;
        }
    }

    _validSlot(value) {
        if (!value || typeof value !== 'object')
            return null;
        const slot = {};
        for (const key of ['x', 'y', 'width', 'height']) {
            if (!Number.isInteger(value[key]))
                return null;
            slot[key] = value[key];
        }
        return slot.width >= 200 && slot.height >= 150 ? slot : null;
    }

    _saveSlot(slot) {
        try {
            GLib.mkdir_with_parents(GLib.path_get_dirname(SLOT_FILE), 0o700);
            GLib.file_set_contents(SLOT_FILE, JSON.stringify(slot));
        } catch (_error) {
            // The slot still applies for this login; only persistence failed.
        }
    }

    _seedReferenceWindow() {
        const windows = this._opencodeWindows();
        if (windows.length === 0)
            return;
        const maintenance = windows.find(window =>
            MAINTENANCE_TITLE.test(window.get_title() ?? ''));
        const recent = windows
            .slice()
            .sort((left, right) => right.get_user_time() - left.get_user_time())[0];
        this._rememberReferenceWindow(maintenance ?? recent);
    }

    _onFocusChanged() {
        const focused = global.display.focus_window;
        if (!focused || !this._isOpenCodeWindow(focused) || this._slot)
            return;
        if (!this._referenceWindowValid())
            this._rememberReferenceWindow(focused);
    }

    _isDeckWindow(window) {
        return PTYXIS_WM_CLASS.test(window.get_wm_class() ?? '') &&
            (window.get_title() ?? '') === DECK_TITLE;
    }

    _readSlotFile(path) {
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            return ok ? this._validSlot(JSON.parse(new TextDecoder().decode(bytes))) : null;
        } catch (_error) {
            return null;
        }
    }

    _placeDeck(window) {
        const slot = this._readSlotFile(DECK_SLOT_FILE);
        if (!slot)
            return false;  // no pin: the launcher's maximize stands
        if (window.maximized_horizontally || window.maximized_vertically)
            window.unmaximize(Meta.MaximizeFlags.HORIZONTAL | Meta.MaximizeFlags.VERTICAL);
        window.move_resize_frame(false, slot.x, slot.y, slot.width, slot.height);
        return true;
    }

    PinDeck() {
        const window = global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null)
            .find(candidate => this._isDeckWindow(candidate));
        if (!window)
            return false;
        const frame = window.get_frame_rect();
        const slot = this._validSlot({x: frame.x, y: frame.y, width: frame.width, height: frame.height});
        if (!slot)
            return false;
        try {
            GLib.mkdir_with_parents(GLib.path_get_dirname(DECK_SLOT_FILE), 0o700);
            GLib.file_set_contents(DECK_SLOT_FILE, JSON.stringify(slot));
        } catch (_error) {
            return false;
        }
        return true;
    }

    _isAgentBrowser(window) {
        if (!window)
            return false;
        if (AGENT_BROWSER_CLASS.test(window.get_wm_class() ?? ''))
            return true;
        // Whatever app id the browser reports, its process uses the agents'
        // own Chrome profile.
        return this._windowProcessArguments(window).some(argument =>
            argument.startsWith('--user-data-dir=') && argument.endsWith(AGENT_BROWSER_PROFILE));
    }

    _agentBrowserConnector() {
        try {
            const [ok, bytes] = GLib.file_get_contents(PLACEMENT_FILE);
            const connector = ok ? JSON.parse(new TextDecoder().decode(bytes)).agentBrowserMonitor : '';
            return typeof connector === 'string' ? connector : '';
        } catch (_error) {
            return '';
        }
    }

    _laptopMonitor() {
        const connector = this._agentBrowserConnector();
        if (!connector)
            return -1;   // not configured: leave the agents' browser alone
        try {
            return global.backend.get_monitor_manager().get_monitor_for_connector(connector);
        } catch (_error) {
            return -1;
        }
    }

    _keepOnLaptop(window) {
        if (!this._isAgentBrowser(window))
            return false;
        const laptop = this._laptopMonitor();
        // Monitor off, missing or not configured: leave the window where GNOME put it.
        if (laptop < 0 || window.get_monitor() === laptop)
            return false;
        window.move_to_monitor(laptop);   // never activates or focuses it
        return true;
    }

    _keepAgentBrowsersOnLaptop() {
        let moved = 0;
        for (const window of global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null)) {
            if (this._keepOnLaptop(window))
                moved++;
        }
        return moved;
    }

    _queueAgentBrowserCheck(window, delay) {
        if (window && !this._isAgentBrowser(window))
            return;
        // Never move a window from inside the signal that reported its move.
        if (this._agentBrowserTimerId)
            GLib.source_remove(this._agentBrowserTimerId);
        this._agentBrowserTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._agentBrowserTimerId = 0;
            this._keepAgentBrowsersOnLaptop();
            return GLib.SOURCE_REMOVE;
        });
    }

    _onWindowCreated(window) {
        for (const delay of [180, 650]) {
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                if (this._keepOnLaptop(window) || this._isAgentBrowser(window))
                    return GLib.SOURCE_REMOVE;
                if (this._isDeckWindow(window)) {
                    this._placeDeck(window);
                    return GLib.SOURCE_REMOVE;
                }
                if (!this._isOpenCodeWindow(window))
                    return GLib.SOURCE_REMOVE;
                if (!this._slot && (MAINTENANCE_TITLE.test(window.get_title() ?? '') ||
                    !this._referenceWindowValid()))
                    this._rememberReferenceWindow(window);
                this._placeWindow(window);
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    _placeWindow(window) {
        if (this._slot)
            return this._placeInSlot(window);
        let area = this._referenceArea;
        let x = this._referenceRect ? this._referenceRect.x : null;
        if (this._referenceWindowValid()) {
            const frame = this._referenceWindow.get_frame_rect();
            area = this._referenceWindow.get_work_area_current_monitor();
            x = frame.x;
            this._referenceRect = {
                x: frame.x,
                y: frame.y,
                width: frame.width,
                height: frame.height,
            };
            this._referenceArea = area;
        }
        if (!area) {
            // No reference yet (e.g. just after login): use the largest screen,
            // your largest monitor, never whichever screen was focused.
            area = this._largestWorkArea(window);
            x = null;
        }
        if (!area)
            return false;
        if (x === null)
            x = area.x;
        if (window.maximized_horizontally || window.maximized_vertically)
            window.unmaximize(Meta.MaximizeFlags.HORIZONTAL | Meta.MaximizeFlags.VERTICAL);
        window.move_resize_frame(false, x, area.y,
            Math.round(area.width * AGENT_TILE_FRACTION), area.height);
        return true;
    }

    _placeInSlot(window) {
        // Follow the pinned window if it is still open (you may have
        // moved it); otherwise use the saved position and size.
        if (this._referenceWindowValid() && this._referenceWindow !== window) {
            const frame = this._referenceWindow.get_frame_rect();
            const current = this._validSlot({x: frame.x, y: frame.y, width: frame.width, height: frame.height});
            if (current && JSON.stringify(current) !== JSON.stringify(this._slot)) {
                this._slot = current;
                this._saveSlot(current);
            }
        }
        if (this._referenceWindow === window)
            return false;
        if (window.maximized_horizontally || window.maximized_vertically)
            window.unmaximize(Meta.MaximizeFlags.HORIZONTAL | Meta.MaximizeFlags.VERTICAL);
        const {x, y, width, height} = this._slot;
        window.move_resize_frame(false, x, y, width, height);
        return true;
    }

    _largestWorkArea(window) {
        const workspace = window.get_workspace?.() ?? global.workspace_manager?.get_active_workspace?.();
        const count = global.display.get_n_monitors?.() ?? 0;
        let best = null;
        for (let monitor = 0; monitor < count; monitor++) {
            const area = workspace?.get_work_area_for_monitor(monitor);
            if (area && (!best || area.width * area.height > best.width * best.height))
                best = area;
        }
        return best;
    }

    SetAgentReference(sessionName) {
        const window = this._tmuxWindow(sessionName);
        if (!window || !this._rememberReferenceWindow(window))
            return false;
        // Pin this window's exact position and size for every new session.
        const slot = this._validSlot(this._referenceRect);
        if (!slot)
            return false;
        this._slot = slot;
        this._saveSlot(slot);
        return true;
    }

    AgentSlot() {
        if (!this._referenceArea)
            return [-1, -1, -1, -1];
        if (this._referenceWindowValid()) {
            const frame = this._referenceWindow.get_frame_rect();
            this._referenceRect = {
                x: frame.x,
                y: frame.y,
                width: frame.width,
                height: frame.height,
            };
            this._referenceArea = this._referenceWindow.get_work_area_current_monitor();
        }
        const x = this._referenceRect ? this._referenceRect.x : -1;
        return [x, this._referenceArea.y,
            Math.round(this._referenceArea.width * AGENT_TILE_FRACTION),
            this._referenceArea.height];
    }

    PlaceViewer(sessionName) {
        const window = this._tmuxWindow(sessionName);
        if (!window)
            return false;
        return this._placeWindow(window);
    }

    _parentPid(pid) {
        try {
            const file = Gio.File.new_for_path(`/proc/${pid}/status`);
            const [loaded, contents] = file.load_contents(null);
            if (!loaded)
                return 0;
            const match = new TextDecoder()
                .decode(contents)
                .match(/^PPid:\s+(\d+)/m);
            return match ? parseInt(match[1], 10) : 0;
        } catch (_error) {
            return 0;
        }
    }

    _ancestorPids(processPid) {
        const ancestors = new Set();
        let current = processPid;
        while (current > 1 && !ancestors.has(current)) {
            ancestors.add(current);
            current = this._parentPid(current);
        }
        return ancestors;
    }

    FocusPid(processPid) {
        // The nearest process that owns a window wins. Terminals OC Deck opens
        // descend from OC Deck's own window, so matching "any ancestor" in
        // most-recently-used order re-focused the deck instead of the terminal.
        const windows = global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null);
        let window = null;
        for (const pid of this._ancestorPids(processPid)) {
            window = windows.find(candidate => candidate.get_pid() === pid);
            if (window)
                break;
        }
        if (!window)
            return false;
        if (window.minimized)
            window.unminimize();
        Main.activateWindow(window);
        return true;
    }
}
