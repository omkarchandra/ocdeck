import Gio from 'gi://Gio';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS = 'org.local.OCDeckNotificationFocus';
const PATH = '/org/local/OCDeckNotificationFocus';
const KEY = 'focus-active-notification';
const MODES = Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW;
const XML = `<node><interface name="${BUS}">
    <method name="Focus"><arg type="b" name="focused" direction="out"/></method>
    <method name="Inspect"><arg type="s" name="state" direction="out"/></method>
</interface></node>`;

export default class OCDeckNotificationFocus extends Extension {
    enable() {
        // GNOME owns the accelerator and its press/release handling. Use the
        // tray's focus grabber so focus survives expansion, hover and timeout.
        Main.wm.setCustomKeybindingHandler(KEY, MODES, () => this.Focus());
        this._object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._object.export(Gio.DBus.session, PATH);
        this._owner = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS, Gio.BusNameOwnerFlags.NONE, null, null);
    }

    _targets(banner) {
        const buttons = banner?._buttonBox?.get_children() ?? [];
        const order = ['Always allow', 'Allow once', 'Reject', 'Open question'];
        const rank = button => {
            const index = order.indexOf(button.label);
            return index < 0 ? order.length : index;
        };
        return [...buttons.filter(button => button.visible && button.reactive)
            .sort((a, b) => rank(a) - rank(b)), banner];
    }

    Focus() {
        if (Main.sessionMode.isLocked)
            return false;
        const tray = Main.messageTray;
        const banner = tray._banner;
        if (!banner)
            return false;

        // Read focus before expanding: GNOME may focus the first button itself.
        // Real actor identity also resets the cycle when a banner is replaced
        // or the user has returned to an application.
        const before = global.stage.get_key_focus();
        tray._expandActiveNotification();
        const targets = this._targets(banner);
        const position = targets.indexOf(before);
        const target = targets[(position + 1) % targets.length];
        target.grab_key_focus();
        return global.stage.get_key_focus() === target;
    }

    Inspect() {
        const banner = Main.messageTray._banner;
        const focused = global.stage.get_key_focus();
        const targets = banner ? this._targets(banner) : [];
        return JSON.stringify({
            title: banner?.notification?.title ?? null,
            id: banner?.notification?.id ?? null,
            actions: targets.filter(target => target !== banner).map(target => target.label),
            focused: targets.includes(focused)
                ? (focused === banner ? 'notification body' : focused.label)
                : null,
        });
    }

    disable() {
        Main.wm.setCustomKeybindingHandler(KEY, MODES,
            Main.messageTray._expandActiveNotification.bind(Main.messageTray));
        if (this._owner)
            Gio.bus_unown_name(this._owner);
        this._owner = 0;
        this._object?.unexport();
        this._object = null;
    }
}
