#!/usr/bin/env python3
"""GNOME-independent OC Deck and OpenCode-agent hotkeys.

Watches kernel evdev devices for hold-(CapsLock|Super) + O and opens OC Deck
or receives F14 from the Kinesis Shift+Tab remap to cycle OpenCode windows.
Bypasses gnome-shell accelerator grabbing, which can silently fail for
media-keys custom shortcuts.
"""
import glob
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

EV_KEY = 1
KEY_O = 24
KEY_F14 = 184
MOD_CODES = {58, 125, 126}
REPEAT = 2
DEDUPE_S = 0.25
DEBUG = os.environ.get("OCDECK_HOTKEY_DEBUG") == "1"
EVENT = struct.Struct("=QQHHi")
WORKSPACE = Path(
    os.environ.get("OCDECK_WORKSPACE", Path(__file__).resolve().parent.parent)
).expanduser()

KEY_NAMES = {
    KEY_O: "KEY_O",
    KEY_F14: "F14",
    58: "CAPS",
    125: "META_L",
    126: "META_R",
}

DBUS_DEST = "org.local.OCDeckSwitch"
DBUS_PATH = "/org/local/OCDeckSwitch"
DBUS_METHOD = "org.local.OCDeckSwitch.LaunchOrCycle"
FOCUS_TMUX_METHOD = "org.local.OCDeckSwitch.FocusTmux"


def dispatch_via_shell():
    """Let GNOME Shell find and focus both Wayland and X11 windows."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/gdbus", "call", "--session",
                "--dest", DBUS_DEST,
                "--object-path", DBUS_PATH,
                "--method", DBUS_METHOD,
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True
        if DEBUG:
            print(f"GNOME dispatch failed: {result.stderr.strip()}", flush=True)
    except Exception as error:
        if DEBUG:
            print(f"GNOME dispatch failed: {error}", flush=True)
    return False


def open_agent_sessions():
    sessions = []
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(path.split("/")[2])
            with open(path, "rb") as cmdline:
                args = [arg for arg in cmdline.read().split(b"\0") if arg]
        except (OSError, ValueError):
            continue
        if not args or os.path.basename(os.fsdecode(args[0])) != "ptyxis":
            continue
        if b"--standalone" not in args or b"attach-session" not in args:
            continue
        try:
            target_index = next(
                index for index, arg in enumerate(args)
                if arg in (b"-t", b"--target-session")
            )
            session = os.fsdecode(args[target_index + 1])
        except (StopIteration, IndexError):
            continue
        if session.startswith("oc-"):
            sessions.append((pid, session))
    return [session for _, session in sorted(sessions)]


def focus_tmux(session):
    try:
        result = subprocess.run(
            [
                "/usr/bin/gdbus", "call", "--session",
                "--dest", DBUS_DEST,
                "--object-path", DBUS_PATH,
                "--method", FOCUS_TMUX_METHOD,
                session,
            ],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and "true" in result.stdout.lower()
    except Exception:
        return False


def cycle_opencode_agent(next_index):
    sessions = open_agent_sessions()
    if not sessions:
        print("No open OpenCode agent windows", flush=True)
        return 0

    for offset in range(len(sessions)):
        index = (next_index + offset) % len(sessions)
        if focus_tmux(sessions[index]):
            print(f"Focused OpenCode agent {sessions[index]}", flush=True)
            return (index + 1) % len(sessions)

    print("Could not focus an OpenCode agent window", flush=True)
    return next_index % len(sessions)


def find_window_id():
    """Most recent visible X11 window titled exactly 'OC Deck'."""
    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "^OC Deck$"],
            capture_output=True, text=True, timeout=5,
        )
        ids = [line for line in result.stdout.split() if line.strip()]
        return int(ids[-1]) if ids else None
    except Exception:
        return None


def focus_existing():
    wid = find_window_id()
    if wid is None:
        return False
    try:
        subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)],
                       timeout=5, check=False)
        return True
    except Exception:
        return False


def launch_new():
    argv = [
        "/usr/bin/ptyxis", "--standalone", "--new-window", "--title=OC Deck",
        "--working-directory=" + str(WORKSPACE),
        "--", "/usr/bin/bash", "-lc",
        'set -a; source "$HOME/.config/opencode/server.env"; '
        'exec "$HOME/.local/bin/ocdeck"',
    ]
    # Run under XWayland so the window is raisable via xdotool next time.
    env = dict(os.environ, GDK_BACKEND="x11")
    subprocess.Popen(argv, start_new_session=True, env=env)


def focus_or_launch():
    if dispatch_via_shell():
        print("Dispatched OC Deck action through GNOME Shell", flush=True)
    elif focus_existing():
        print("Focused existing OC Deck", flush=True)
    else:
        print("Launching new OC Deck", flush=True)
        launch_new()


def add_devices(watchers):
    existing = set(watchers.values())
    for path in sorted(glob.glob("/dev/input/event*")):
        if path in existing:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        watchers[fd] = path


def open_ocdeck():
    argv = [
        "/usr/bin/ptyxis", "--standalone", "--new-window", "--title=OC Deck",
        "--working-directory=" + str(WORKSPACE),
        "--", "/usr/bin/bash", "-lc",
        'set -a; source "$HOME/.config/opencode/server.env"; '
        'exec "$HOME/.local/bin/ocdeck"',
    ]
    subprocess.Popen(argv, start_new_session=True)


def main():
    watchers = {}
    add_devices(watchers)

    if not watchers:
        print("No input devices could be opened (input group?).", file=sys.stderr)
        sys.exit(1)

    print(
        f"Watching {len(watchers)} evdev devices; Caps/Super+O opens OC Deck, "
        "Shift+Tab cycles OpenCode agents.",
        flush=True,
    )

    mods = set()
    last_o_down = 0.0
    last_f14_down = 0.0
    next_agent = 0
    last_scan = 0.0

    try:
        while True:
            now = time.monotonic()
            if now - last_scan >= 5:
                add_devices(watchers)
                last_scan = now
            readable, _, _ = select.select(list(watchers), [], [], 5)
            now = time.monotonic()
            for fd in readable:
                try:
                    data = os.read(fd, EVENT.size * 32)
                except OSError:
                    watchers.pop(fd, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    continue
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, etype, code, value = EVENT.unpack_from(data, offset)
                    if etype != EV_KEY:
                        continue
                    if DEBUG and value != REPEAT:
                        print(
                            f"key {KEY_NAMES.get(code, code)} {'down' if value == 1 else 'up'} "
                            f"mods={sorted(mods)} dev={watchers[fd]}",
                            flush=True,
                        )
                    if code in MOD_CODES:
                        if value == 1:
                            mods.add(code)
                        elif value == 0:
                            mods.discard(code)
                    elif code == KEY_O and value == 1:
                        if mods and now - last_o_down > DEDUPE_S:
                            last_o_down = now
                            focus_or_launch()
                    elif code == KEY_F14 and value == 1:
                        if now - last_f14_down > DEDUPE_S:
                            last_f14_down = now
                            next_agent = cycle_opencode_agent(next_agent)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
