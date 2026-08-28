import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import GObject from 'gi://GObject';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';

const SCHEMA = 'org.gnome.shell.extensions.workspace-tiling';
const OBSIDIAN_WM_CLASS = /(^|\.)obsidian$/i;

function loadSettings(extension) {
    const schemaDir = GLib.build_filenamev([extension.metadata.dir.get_path(), 'schemas']);
    const source = Gio.SettingsSchemaSource.new_from_directory(schemaDir,
        Gio.SettingsSchemaSource.get_default(), false);
    const schema = source.lookup(SCHEMA, false);
    if (!schema)
        throw new Error(`Schema ${SCHEMA} not found in ${schemaDir}`);
    return new Gio.Settings({settings_schema: schema});
}

const TILE_ACTIONS = [
    {key: 'cycle-quarter', label: 'Cycle 1/4 positions'},
    {key: 'cycle-quarterv', label: 'Cycle vertical 1/4 columns'},
    {key: 'cycle-half', label: 'Cycle 1/2 positions'},
    {key: 'cycle-third', label: 'Cycle 1/3 positions'},
    {key: 'cycle-threequarter', label: 'Cycle 3/4 positions'},
];

const FIXED_ACTIONS = [
    {key: 'half-left', label: 'Left half'},
    {key: 'half-right', label: 'Right half'},
    {key: 'half-up', label: 'Top half'},
    {key: 'half-down', label: 'Bottom half'},
];

const PRESETS = {
    'cycle-quarter': [[0, 0, .5, .5], [.5, 0, .5, .5], [0, .5, .5, .5], [.5, .5, .5, .5]],
    // Full-height quarter-width columns, left to right.
    'cycle-quarterv': [[0, 0, .25, 1], [.25, 0, .25, 1], [.5, 0, .25, 1], [.75, 0, .25, 1]],
    'cycle-half': [[0, 0, .5, 1], [.5, 0, .5, 1], [0, 0, 1, .5], [0, .5, 1, .5]],
    'cycle-third': [[0, 0, 1 / 3, 1], [1 / 3, 0, 1 / 3, 1], [2 / 3, 0, 1 / 3, 1]],
    'cycle-threequarter': [[0, 0, .75, 1], [.25, 0, .75, 1]],
    'half-left': [0, 0, .5, 1],
    'half-right': [.5, 0, .5, 1],
    'half-up': [0, 0, 1, .5],
    'half-down': [0, .5, 1, .5],
};

const APP_ACTIONS = [
    {key: 'hyper-6', num: 6, label: 'App 6'},
    {key: 'hyper-7', num: 7, label: 'App 7'},
    {key: 'hyper-8', num: 8, label: 'App 8'},
];

const MODIFIER_KEYNAMES = {
    Shift_L: 1, Shift_R: 1, Control_L: 1, Control_R: 1,
    Alt_L: 1, Alt_R: 1, Meta_L: 1, Meta_R: 1,
    Super_L: 1, Super_R: 1, Hyper_L: 1, Hyper_R: 1,
    ISO_Level2_Latch: 1, ISO_Level3_Shift: 1, ISO_Level3_Latch: 1,
    ISO_Level5_Shift: 1, ISO_Next_Group: 1,
};

function accelText(strv) {
    return strv && strv.length > 0 ? strv.join(' or ') : '(unset)';
}

const CaptureDialog = GObject.registerClass(
class CaptureDialog extends ModalDialog.ModalDialog {
    _init() {
        super._init();
        this._callback = null;
        const box = new St.BoxLayout({vertical: true, style: 'spacing: 12px;'});
        this._title = new St.Label({
            text: '',
            x_align: Clutter.ActorAlign.CENTER,
            style: 'font-weight: bold; font-size: 1.2em;',
        });
        const hint = new St.Label({
            text: 'Press the new key combination.\nPress Esc to cancel.',
            x_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._title);
        box.add_child(hint);
        this.contentLayout.add_child(box);
    }

    openFor(title, callback) {
        this._title.text = title;
        this._callback = callback;
        this.open(global.get_current_time());
    }

    vfunc_key_press_event(event) {
        const [, keyval] = event.get_keyval();
        let mods = event.get_state() & Clutter.ModifierType.MODIFIER_MASK;
        const name = Clutter.keyval_name(keyval);
        if (!name)
            return Clutter.EVENT_STOP;

        if (MODIFIER_KEYNAMES[name])
            return Clutter.EVENT_STOP;

        if (name === 'Escape') {
            this.close(global.get_current_time());
            if (this._callback)
                this._callback(null);
            return Clutter.EVENT_STOP;
        }

        mods &= (Clutter.ModifierType.CONTROL_MASK | Clutter.ModifierType.SHIFT_MASK |
                 Clutter.ModifierType.ALT_MASK | Clutter.ModifierType.SUPER_MASK |
                 Clutter.ModifierType.HYPER_MASK | Clutter.ModifierType.META_MASK);

        const parts = [];
        if (mods & Clutter.ModifierType.CONTROL_MASK) parts.push('<Control>');
        if (mods & Clutter.ModifierType.SHIFT_MASK) parts.push('<Shift>');
        if (mods & Clutter.ModifierType.ALT_MASK) parts.push('<Alt>');
        if (mods & Clutter.ModifierType.SUPER_MASK) parts.push('<Super>');
        if (mods & Clutter.ModifierType.HYPER_MASK) parts.push('<Hyper>');
        if (mods & Clutter.ModifierType.META_MASK) parts.push('<Meta>');
        parts.push(name.length === 1 ? name.toLowerCase() : name);

        this.close(global.get_current_time());
        if (this._callback)
            this._callback(parts.join(''));
        return Clutter.EVENT_STOP;
    }
});

const WorkspaceTilingIndicator = GObject.registerClass(
class WorkspaceTilingIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Window Tiling', false);
        this._extension = extension;
        this._rows = [];
        this.add_child(new St.Icon({
            icon_name: 'view-grid-symbolic',
            style_class: 'system-status-icon',
        }));

        TILE_ACTIONS.forEach(a => this.menu.addMenuItem(this._makeActionRow(a)));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        FIXED_ACTIONS.forEach(a => this.menu.addMenuItem(this._makeActionRow(a)));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        APP_ACTIONS.forEach(a => this.menu.addMenuItem(this._makeAppRow(a)));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const rebind = new PopupMenu.PopupSubMenuMenuItem('Change keybindings\u2026');
        [...TILE_ACTIONS, ...FIXED_ACTIONS].forEach(a =>
            rebind.menu.addMenuItem(this._makeBindRow(a)));
        APP_ACTIONS.forEach(a => rebind.menu.addMenuItem(this._makeBindRow(a)));
        this.menu.addMenuItem(rebind);

        const tip = new PopupMenu.PopupMenuItem(
            'Each press moves the window to the next position of that size.',
            {reactive: false, style_class: 'popup-menu-item'});
        tip.label.get_clutter_text().set_line_wrap(true);
        tip.label.set_style('opacity:.65;font-size:.95em;');
        this.menu.addMenuItem(tip);
    }

    _makeActionRow(action) {
        const item = new PopupMenu.PopupMenuItem(action.label);
        const accel = new St.Label({text: '', style: 'opacity:.7;'});
        accel.x_expand = true;
        accel.x_align = Clutter.ActorAlign.END;
        item.add_child(accel);
        item.connect('activate', () => this._extension.runTiling(action.key));
        this._rows.push({action, item, accel});
        return item;
    }

    _makeAppRow(action) {
        const id = this._extension.settings.get_string(`app-id-${action.num}`);
        const item = new PopupMenu.PopupMenuItem(`${action.label}: ${id.split('.')[0]}`);
        const accel = new St.Label({text: '', style: 'opacity:.7;'});
        accel.x_expand = true;
        accel.x_align = Clutter.ActorAlign.END;
        item.add_child(accel);
        item.connect('activate', () => this._extension.focusApp(action.num));
        this._rows.push({action, item, accel});
        return item;
    }

    _makeBindRow(action) {
        const item = new PopupMenu.PopupMenuItem('');
        item.connect('activate', () => this._extension.startCapture(action));
        this._rows.push({action, item, accel: null});
        return item;
    }

    refresh() {
        for (const row of this._rows) {
            const current = this._extension.settings.get_strv(row.action.key);
            if (row.accel)
                row.accel.text = accelText(current);
            row.item.label.text =
                `${row.action.label}: ${accelText(current)} \u2014 click to rebind`;
        }
    }
});

export default class WorkspaceTilingExtension extends Extension {
    enable() {
        try {
            this.settings = loadSettings(this);
            this._indicator = null;
            this._dialog = null;
            this._boundKeys = [];
            this._refreshQueued = false;

            this._settingsChangedId = this.settings.connect('changed', () =>
                this._queueRefresh());

            if (this.settings.get_boolean('show-indicator')) {
                this._indicator = new WorkspaceTilingIndicator(this);
                Main.panel.addToStatusArea(this.uuid, this._indicator);
                this._indicator.refresh();
            }

            this._dialog = new CaptureDialog();
            this._grabAll();
        } catch (e) {
            console.error(`workspace-tiling: enable failed: ${e}`);
            Main.notify('Workspace Tiling', `Failed to start: ${e}`);
        }
    }

    disable() {
        if (this._settingsChangedId) {
            this.settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = null;
        }
        for (const key of this._boundKeys) {
            try {
                Main.wm.removeKeybinding(key);
            } catch {
                /* already gone */
            }
        }
        this._boundKeys = [];
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        if (this._dialog) {
            this._dialog.destroy();
            this._dialog = null;
        }
    }

    _grabAll() {
        const failed = [];
        const defs = [...TILE_ACTIONS, ...FIXED_ACTIONS, ...APP_ACTIONS];
        for (const def of defs) {
            const accels = this.settings.get_strv(def.key);
            if (!accels || accels.length === 0)
                continue;
            try {
                const ok = Main.wm.addKeybinding(def.key, this.settings,
                    Meta.KeyBindingFlags.NONE, Shell.ActionMode.NORMAL,
                    () => this._dispatch(def.key));
                this._boundKeys.push(def.key);
                if (!ok)
                    failed.push(`${def.label} (${accelText(accels)})`);
            } catch (e) {
                failed.push(`${def.label}: ${e}`);
            }
        }
        if (failed.length > 0)
            Main.notify('Workspace Tiling', 'Could not grab:\n' + failed.join('\n'));
    }

    _regrabAll() {
        for (const key of this._boundKeys) {
            try {
                Main.wm.removeKeybinding(key);
            } catch {
                /* ignore */
            }
        }
        this._boundKeys = [];
        this._grabAll();
    }

    _queueRefresh() {
        if (this._refreshQueued)
            return;
        this._refreshQueued = true;
        GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._refreshQueued = false;
            this._regrabAll();
            if (this._indicator)
                this._indicator.refresh();
            return GLib.SOURCE_REMOVE;
        });
    }

    _dispatch(key) {
        if (key.startsWith('hyper-'))
            this.focusApp(parseInt(key.split('-')[1]));
        else
            this.runTiling(key);
    }

    runTiling(key) {
        const preset = PRESETS[key];
        if (!preset)
            return;
        const win = global.display.get_focus_window();
        if (!win || win.get_window_type() !== Meta.WindowType.NORMAL)
            return;

        if (win.maximized_horizontally || win.maximized_vertically)
            win.unmaximize(Meta.MaximizeFlags.HORIZONTAL | Meta.MaximizeFlags.VERTICAL);

        const area = win.get_work_area_current_monitor();
        const frame = win.get_frame_rect();

        let target;
        if (Array.isArray(preset[0])) {
            const tolerance = Math.max(24, Math.round(area.width * 0.03));
            const matches = r => t => Math.abs(r.x - t.x) <= tolerance &&
                Math.abs(r.y - t.y) <= tolerance &&
                Math.abs(r.width - t.width) <= tolerance &&
                Math.abs(r.height - t.height) <= tolerance;
            const absolute = preset.map(t => ({
                x: Math.round(area.x + t[0] * area.width),
                y: Math.round(area.y + t[1] * area.height),
                width: Math.round(t[2] * area.width),
                height: Math.round(t[3] * area.height),
            }));
            const current = absolute.findIndex(matches(frame));
            target = absolute[(current + 1) % absolute.length];
        } else {
            target = {
                x: Math.round(area.x + preset[0] * area.width),
                y: Math.round(area.y + preset[1] * area.height),
                width: Math.round(preset[2] * area.width),
                height: Math.round(preset[3] * area.height),
            };
        }

        win.move_resize_frame(false, target.x, target.y, target.width, target.height);
    }

    focusApp(num) {
        const id = this.settings.get_string(`app-id-${num}`);
        const app = Shell.AppSystem.get_default().lookup_app(id);
        if (!app) {
            Main.notify('Workspace Tiling', `Application not found: ${id}`);
            return;
        }
        const workspace = global.workspace_manager.get_active_workspace();
        let windows = app.get_windows();
        if (num === 8 && windows.length === 0) {
            windows = global.display
                .get_tab_list(Meta.TabList.NORMAL_ALL, null)
                .filter(window => OBSIDIAN_WM_CLASS.test(window.get_wm_class() ?? ''));
        }
        const mostRecent = wins => wins
            .slice()
            .sort((a, b) => b.get_user_time() - a.get_user_time())[0];
        const target = mostRecent(windows.filter(w => w.get_workspace() === workspace)) ||
                       mostRecent(windows);
        if (target) {
            target.unminimize();
            target.activate(global.get_current_time());
        } else {
            app.activate();
        }
    }

    startCapture(action) {
        if (!this._dialog)
            return;
        const niceName = action.num
            ? `${action.label} (${this.settings.get_string(`app-id-${action.num}`)})`
            : action.label;
        this._dialog.openFor(`New shortcut for: ${niceName}`, accel => {
            if (accel)
                this.settings.set_strv(action.key, [accel]);
        });
    }
}
