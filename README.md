# opencode-permission-notify

An [opencode](https://opencode.ai) plugin that turns permission prompts and questions into persistent, actionable notifications for the exact Ptyxis tab where attention is needed.

## Why

Ptyxis doesn't expose tab IDs publicly, and AppArmor blocks plain `notify-send` actions. This plugin works around that by using Ptyxis's own notification pipeline.

## How it works

1. On a `permission.asked` event, the plugin briefly emits VTE shell markers (`vte.shell.preexec` / `vte.shell.precmd`) so Ptyxis generates its own "Command completed" notification.
2. It reads the tab UUID off that notification from the D-Bus bus (`org.gtk.Notifications.AddNotification`).
3. It replaces it with an urgent OC Deck-owned "OpenCode permission" notification, which remains visible until the permission is answered. Keeping notification ownership separate from Ptyxis prevents notification actions from opening blank terminals.
4. A `question.asked` event creates its own urgent **OpenCode question** tile with an **Open question** action. It remains until the question is answered or dismissed.
5. The **Allow once** and **Always allow** buttons grant permissions through opencode. Clicking any permission or question notification focuses the exact tmux-backed Ptyxis window through the OC Deck GNOME extension, with the captured Ptyxis tab UUID as a fallback.
6. If the tmux session has no viewer, clicking opens a viewer attached to that exact session instead of launching a blank terminal.

## Keyboard navigation

- `Super+N` (physical `CapsLock+N` when Caps Lock is remapped to Super) runs a native C client, which asks a resident C AT-SPI listener to focus the visible permission banner's **Always allow** control without activating it.
- Permission actions are ordered **Always allow**, **Allow once**, then the notification body. `Tab` and `Shift+Tab` move between controls; `Enter` activates the focused item and focuses the exact requesting tmux-backed Ptyxis window.
- `Super+V` opens the full notification list when you need to move between multiple notification tiles.

The workstation uses a GNOME custom keybinding for `Super+N` because GNOME 50's stock `focus-active-notification` handler focuses the banner container instead of an action button. The resident listener caches notification controls as they appear, so the keypress does not launch Python or traverse the accessibility tree. It also observes notification activation and maps the notification ID to the owning permission state before requesting exact tmux focus. The stock binding is left empty to avoid an accelerator conflict.

## Top-right placement

GNOME Shell controls notification placement. To keep the label at the top right, use a compatible notification-position extension and select **Top** and **Right**. The plugin's urgent priority controls how long the label remains visible, not its screen position.

## Install

Copy `plugins/permission-notify.js` to `~/.config/opencode/plugins/` and restart opencode:

```sh
mkdir -p ~/.config/opencode/plugins
rm -f ~/.config/opencode/plugins/permission-notify.js
rm -f ~/.config/opencode/plugins/permission-notify-v2.js
rm -f ~/.config/opencode/plugins/permission-notify-v3.js
rm -f ~/.config/opencode/plugins/permission-notify-v4.js
rm -f ~/.config/opencode/plugins/permission-notify-v5.js
rm -f ~/.config/opencode/plugins/permission-notify-v6.js
cp plugins/permission-notify.js ~/.config/opencode/plugins/permission-notify-v7.js
install -Dm644 plugins/org.local.OCDeckSwitch.desktop \
  ~/.local/share/applications/org.local.OCDeckSwitch.desktop
```

## Requirements

- Ptyxis terminal (50.x) with shell integration markers supported
- GNOME Shell session (`org.gtk.Notifications` on the session bus)
- GLib and AT-SPI runtime libraries for the native notification-control focus service
- `gdbus` and `busctl` on PATH
