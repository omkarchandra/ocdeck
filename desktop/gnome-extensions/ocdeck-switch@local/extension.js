import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const LAUNCH_SWITCH_KEYBINDING = 'launch-switch';
const OCDECK_TITLE = 'OC Deck';
const PTYXIS_WM_CLASS = /(^|\.)ptyxis$/i;
const DBUS_NAME = 'org.local.OCDeckSwitch';
const DBUS_PATH = '/org/local/OCDeckSwitch';
const DBUS_XML = `
<node>
  <interface name="${DBUS_NAME}">
    <method name="LaunchOrCycle">
      <arg type="b" name="handled" direction="out"/>
    </method>
    <method name="Inspect">
      <arg type="s" name="state" direction="out"/>
    </method>
    <method name="FocusTmux">
      <arg type="s" name="session_name" direction="in"/>
      <arg type="b" name="focused" direction="out"/>
    </method>
  </interface>
</node>`;
const OCDECK_COMMAND = 'exec "$HOME/.local/bin/ocdeck-entrypoint"';

export default class OCDeckSwitchExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._cycleIndex = 0;
        this._keybindingAdded = false;

        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(DBUS_XML, this);
        this._dbusObject.export(Gio.DBus.session, DBUS_PATH);
        this._dbusOwnerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session,
            DBUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null);

        if (this._settings.get_strv(LAUNCH_SWITCH_KEYBINDING).length > 0) {
            Main.wm.addKeybinding(
                LAUNCH_SWITCH_KEYBINDING,
                this._settings,
                Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
                () => this._launchOrCycle());
            this._keybindingAdded = true;
        }
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

    FocusTmux(sessionName) {
        const window = this._tmuxWindow(sessionName);
        if (!window)
            return false;
        if (window.minimized)
            window.unminimize();
        Main.activateWindow(window);
        this._pulseWindow(window);
        return true;
    }

    LaunchOrCycle() {
        return this._launchOrCycle();
    }

    Inspect() {
        return JSON.stringify(this._ocdeckWindows().map(window => ({
            pid: window.get_pid(),
            title: window.get_title(),
            focused: window === global.display.focus_window,
            minimized: window.minimized,
        })));
    }

    _pulseWindow(window) {
        const actor = window.get_compositor_private();
        if (!actor)
            return;

        actor.set_pivot_point(0.5, 0.5);
        actor.ease({
            scale_x: 1.025,
            scale_y: 1.025,
            duration: 110,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                if (!actor.get_stage())
                    return;
                actor.ease({
                    scale_x: 1,
                    scale_y: 1,
                    duration: 180,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
                });
            },
        });
    }

    _ocdeckWindows() {
        return global.display
            .get_tab_list(Meta.TabList.NORMAL_ALL, null)
            .filter(window => {
                if (!PTYXIS_WM_CLASS.test(window.get_wm_class() ?? ''))
                    return false;
                const title = (window.get_title() ?? '').toLowerCase();
                if (title === OCDECK_TITLE.toLowerCase())
                    return true;

                const args = this._windowProcessArguments(window);
                if (args.includes('attach-session'))
                    return false;
                // Ptyxis updates its window title to the foreground command.
                // Keep recognizing our dedicated launcher after that happens.
                const titledLauncher = args.includes('--title=OC Deck') ||
                    args.some((argument, index) =>
                        argument === '--title' && args[index + 1] === OCDECK_TITLE);
                if (titledLauncher && args.includes(OCDECK_COMMAND))
                    return true;
                return args.some(argument =>
                    argument === 'ocdeck' || argument.endsWith('/ocdeck'));
            })
            .sort((left, right) =>
                left.get_stable_sequence() - right.get_stable_sequence());
    }

    _launchOrCycle() {
        const windows = this._ocdeckWindows();
        if (windows.length === 0) {
            const now = GLib.get_monotonic_time();
            if (now < (this._launchPendingUntil ?? 0))
                return true;
            this._launchPendingUntil = now + 3_000_000;
            Gio.Subprocess.new([
                '/usr/bin/ptyxis',
                '--standalone',
                '--tab',
                '--title',
                OCDECK_TITLE,
                `--working-directory=${GLib.get_home_dir()}`,
                '--',
                '/usr/bin/bash',
                '-lc',
                OCDECK_COMMAND,
            ], Gio.SubprocessFlags.NONE);
            this._cycleIndex = 0;
            return true;
        }

        this._launchPendingUntil = 0;

        const focusedIndex = windows.findIndex(
            window => window === global.display.focus_window);
        const targetIndex = focusedIndex >= 0
            ? (focusedIndex + 1) % windows.length
            : this._cycleIndex % windows.length;
        this._cycleIndex = (targetIndex + 1) % windows.length;
        const target = windows[targetIndex];
        if (target.minimized)
            target.unminimize();
        Main.activateWindow(target);
        this._pulseWindow(target);
        return true;
    }

    disable() {
        if (this._keybindingAdded)
            Main.wm.removeKeybinding(LAUNCH_SWITCH_KEYBINDING);
        this._keybindingAdded = false;
        if (this._dbusOwnerId) {
            Gio.bus_unown_name(this._dbusOwnerId);
            this._dbusOwnerId = 0;
        }
        this._dbusObject?.unexport();
        this._dbusObject = null;
        this._settings = null;
    }
}
