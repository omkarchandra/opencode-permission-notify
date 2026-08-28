import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const QUARTERS_KEYBINDING = 'toggle-quarters';
const THIRD_KEYBINDING = 'toggle-thirds';
const THREE_QUARTERS_KEYBINDING = 'toggle-three-quarters';
const INNER_GAP = 8;
const OUTER_GAP = 8;
const MATCH_TOLERANCE = 12;

export default class WindowFractionToggleExtension extends Extension {
    enable() {
        this._settings = this.getSettings();

        this._addKeybinding(QUARTERS_KEYBINDING, () => this._cycle(4, 1));
        this._addKeybinding(THIRD_KEYBINDING, () => this._cycle(3, 1));
        this._addKeybinding(THREE_QUARTERS_KEYBINDING, () => this._cycle(4, 3));
    }

    disable() {
        Main.wm.removeKeybinding(QUARTERS_KEYBINDING);
        Main.wm.removeKeybinding(THIRD_KEYBINDING);
        Main.wm.removeKeybinding(THREE_QUARTERS_KEYBINDING);
        this._settings = null;
    }

    _addKeybinding(name, handler) {
        Main.wm.addKeybinding(
            name,
            this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL,
            handler
        );
    }

    // Repeated presses cycle the focused window through every position of
    // `span` adjacent columns within `columns` (left to right, then wrap).
    // The current slot is detected from the frame rect, so manual moves or
    // switching between keybindings stay consistent.
    _cycle(columns, span) {
        const window = global.display.focusWindow;
        if (!window || !window.allows_move() || !window.allows_resize())
            return;

        if (window.fullscreen)
            window.unmake_fullscreen();
        if (window.maximizedHorizontally || window.maximizedVertically)
            window.unmaximize();

        const workArea = window.get_work_area_current_monitor();
        const availableWidth = workArea.width
            - (columns - 1) * INNER_GAP
            - 2 * OUTER_GAP;
        const columnWidth = availableWidth / columns;
        const width = Math.round(
            span * columnWidth + (span - 1) * INNER_GAP
        );
        const y = Math.round(workArea.y + OUTER_GAP);
        const height = Math.round(workArea.height - 2 * OUTER_GAP);

        const xs = [];
        for (let col = 0; col + span <= columns; col++) {
            xs.push(Math.round(
                workArea.x + OUTER_GAP + col * (columnWidth + INNER_GAP)
            ));
        }

        const current = window.get_frame_rect();
        let index = xs.findIndex(x =>
            Math.abs(current.x - x) <= MATCH_TOLERANCE
            && Math.abs(current.width - width) <= MATCH_TOLERANCE);
        const next = xs[(index + 1) % xs.length];

        window.move_frame(true, next, y);
        window.move_resize_frame(true, next, y, width, height);
    }
}
