import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

// The same serialized, tab-aware route as the Super+O shortcut.
const HOTKEY = GLib.build_filenamev([GLib.get_home_dir(), '.local/bin/ocdeck-hotkey']);

export default class OCDeckButtonExtension extends Extension {
    enable() {
        this._running = false;
        // Shell 50 needs a PanelMenu.Button; the third argument turns off its
        // menu so the click is ours.
        this._button = new PanelMenu.Button(0.0, 'OC Deck', true);
        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({icon_name: 'utilities-terminal-symbolic', style_class: 'system-status-icon'}));
        box.add_child(new St.Label({text: 'Deck', y_align: Clutter.ActorAlign.CENTER}));
        this._button.add_child(box);
        this._button.connect('button-press-event', () => {
            this._open();
            return Clutter.EVENT_STOP;
        });
        Main.panel.addToStatusArea('ocdeck-button', this._button, 0, 'right');
    }

    _open() {
        if (this._running)
            return;  // a dispatch can take a few seconds; ignore extra clicks
        this._running = true;
        try {
            const process = Gio.Subprocess.new([HOTKEY, '--dispatch'], Gio.SubprocessFlags.NONE);
            process.wait_async(null, () => { this._running = false; });
        } catch (error) {
            this._running = false;
            Main.notify('OC Deck', `Could not run ocdeck-hotkey: ${error.message}`);
        }
    }

    disable() {
        this._button?.destroy();
        this._button = null;
    }
}
