from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


EXIT_ERROR = 2
EXIT_MISSING = 3


def ptyxis_pid_for_tmux(session_name: str) -> int | None:
    encoded_name = os.fsencode(session_name)
    for entry in sorted(Path("/proc").iterdir(), key=lambda path: path.name):
        if not entry.name.isdigit():
            continue
        try:
            arguments = [
                value
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except OSError:
            continue
        if not arguments or Path(os.fsdecode(arguments[0])).name != "ptyxis":
            continue
        if b"--standalone" not in arguments or b"attach-session" not in arguments:
            continue
        if any(
            argument == b"-t" and index + 1 < len(arguments)
            and arguments[index + 1] == encoded_name
            for index, argument in enumerate(arguments)
        ):
            return int(entry.name)
    return None


def bus_name_for_pid(connection: Gio.DBusConnection, pid: int) -> str:
    names_reply = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "ListNames",
        None,
        GLib.VariantType.new("(as)"),
        Gio.DBusCallFlags.NONE,
        3000,
        None,
    )
    for name in names_reply.unpack()[0]:
        if not name.startswith(":"):
            continue
        try:
            pid_reply = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (name,)),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        except GLib.Error:
            continue
        if pid_reply.unpack()[0] == pid:
            return name
    return ""


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1]:
        return EXIT_ERROR
    pid = ptyxis_pid_for_tmux(sys.argv[1])
    if pid is None:
        return EXIT_MISSING
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus_name = bus_name_for_pid(connection, pid)
        if not bus_name:
            return EXIT_ERROR
        connection.call_sync(
            bus_name,
            "/org/gnome/Ptyxis",
            "org.gtk.Application",
            "Activate",
            GLib.Variant("(a{sv})", ({},)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
    except GLib.Error:
        return EXIT_ERROR
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
