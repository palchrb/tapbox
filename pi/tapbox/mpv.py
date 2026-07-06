"""mpv IPC socket client — talks to the player.py-owned mpv instance."""

import json
import os
import socket

SOCK = os.environ.get("TAPBOX_MPV_SOCK", "/run/tapbox-mpv.sock")


def ipc(command, sock=None, timeout=2):
    """Send one command; returns mpv's reply dict ({} if none). mpv
    interleaves async events with replies — the reply is the line with
    an "error" field. Raises OSError when the socket is gone."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(sock or SOCK)
        s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
        for line in s.recv(65536).split(b"\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "error" in msg:
                return msg
    return {}


def get(prop, sock=None):
    """A property value, or None (also on any socket error)."""
    try:
        r = ipc(["get_property", prop], sock=sock)
    except (OSError, ValueError):
        return None
    return r.get("data") if r.get("error") == "success" else None


def playing(sock=None):
    """True only when mpv is up and not paused."""
    return get("pause", sock=sock) is False
