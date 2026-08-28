import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const FOCUS_OBSIDIAN_KEYBINDING = 'focus-obsidian';
const RESTORE_ALL_WINDOWS_KEYBINDING = 'restore-all-windows';
const OBSIDIAN_WM_CLASS = /(^|\.)obsidian$/i;
const OBSIDIAN_APP_IDS = [
    'md.obsidian.Obsidian.desktop',
    'obsidian_md.obsidian.Obsidian.desktop',
];

export default class CapsObsidianExtension extends Extension {
    enable() {
        this._settings = this.getSettings();

        Main.wm.addKeybinding(
            FOCUS_OBSIDIAN_KEYBINDING,
            this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => {
                const appSystem = Shell.AppSystem.get_default();
                const app = OBSIDIAN_APP_IDS
                    .map(id => appSystem.lookup_app(id))
                    .find(candidate => candidate !== null);
                const window = app?.get_windows()[0] ?? global.display
                    .get_tab_list(Meta.TabList.NORMAL_ALL, null)
                    .find(candidate =>
                        OBSIDIAN_WM_CLASS.test(candidate.get_wm_class() ?? ''));

                if (window)
                    Main.activateWindow(window);
                else
                    app?.activate();
            });

        Main.wm.addKeybinding(
            RESTORE_ALL_WINDOWS_KEYBINDING,
            this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => {
                const workspace = global.workspace_manager.get_active_workspace();
                const windows = workspace.list_windows();
                const target = global.display
                    .get_tab_list(Meta.TabList.NORMAL, workspace)[0];

                // Activating a normal window also exits Mutter's show-desktop mode.
                if (target)
                    Main.activateWindow(target);

                for (const window of windows) {
                    if (window.minimized)
                        window.unminimize();
                }
            });
    }

    disable() {
        Main.wm.removeKeybinding(FOCUS_OBSIDIAN_KEYBINDING);
        Main.wm.removeKeybinding(RESTORE_ALL_WINDOWS_KEYBINDING);
        this._settings = null;
    }
}
