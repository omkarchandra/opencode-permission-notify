import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

const PTT_KEYBINDING = 'push-to-talk';
const HELPER = `${GLib.get_home_dir()}/.local/bin/voice-ptt`;
const PIDFILE = '/tmp/voice_ptt.pid';
const MODE_FILE = '/tmp/voice_ptt_mode';

export default class VoiceLaunchExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._recording = false;

        // Shell 50 requires a PanelMenu.Button and no longer emits 'clicked';
        // third arg disables the built-in menu gesture so clicks are ours.
        this._button = new PanelMenu.Button(0.0, 'Voice', true);
        this._label = new St.Label({text: '\u{1F3A4} Voice'});
        this._button.add_child(this._label);
        this._button.connect('button-press-event', () => {
            this._toggle();
            return Clutter.EVENT_STOP;
        });
        this._sync();
        Main.panel.addToStatusArea('voice-launch', this._button);

        // Tap-toggle: first press starts recording, second press sends.
        Main.wm.addKeybinding(
            PTT_KEYBINDING,
            this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._toggle());
    }

    _start() {
        if (this._recording)
            return;
        Shell.util_spawn_command_line_async(HELPER);
        this._recording = true;
        this._sync();
    }

    _send() {
        if (!this._recording)
            return;
        this._recording = false;

        try {
            Gio.File.new_for_path(MODE_FILE).replace_contents(
                'agent', null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        } catch (e) {
            // The helper's existing fallback is agent mode.
        }

        Shell.util_spawn_command_line_async(`kill -USR1 $(cat ${PIDFILE} 2>/dev/null)`);
        this._sync();
    }

    _toggle() {
        if (this._recording)
            this._send();
        else
            this._start();
    }

    _sync() {
        if (this._label)
            this._label.set_text(this._recording ? '\u{1F3A4} Voice \u{25CF}' : '\u{1F3A4} Voice');
    }

    disable() {
        Main.wm.removeKeybinding(PTT_KEYBINDING);
        if (this._button) {
            this._button.destroy();
            this._button = null;
        }
        this._settings = null;
    }
}
