"""mpv IPC socket client — talks to the player.py-owned mpv instance."""

import json
import os
import socket
import time

SOCK = os.environ.get("VIBB_MPV_SOCK", "/run/vibb-mpv.sock")


# How long to keep reading AFTER the first chunk, waiting for the reply
# to appear among mpv's event traffic. Deliberately much shorter than
# `timeout`: several callers run under the daemon's lock, and a control
# press that cannot take that lock within a second is DROPPED — so a
# slow read here costs button presses, not just latency.
DRAIN_S = float(os.environ.get("VIBB_MPV_DRAIN", "0.4"))


def ipc(command, sock=None, timeout=2):
    """Send one command; returns mpv's reply dict ({} if none). mpv
    interleaves async events with replies — the reply is the line with
    an "error" field. Raises OSError when the socket is gone.

    Reads until the reply appears rather than trusting one recv(): mpv
    broadcasts end-file/start-file/playback-restart to EVERY client, so
    during a track-change storm — precisely when the callers of this
    function are trying to work out whether the output has died — the
    reply can land in the second read and a single-chunk read returns
    {} (QA 2026-08-13). Bounded by DRAIN_S so it cannot hold a lock.
    """
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(sock or SOCK)
        s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
        buf, deadline = b"", None
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if deadline is None:
                    raise              # mpv never answered at all: a real
                #                        socket error, as before
                break                  # events came, the reply did not
            if not chunk:
                break                      # mpv closed the connection
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines.pop()              # keep any partial last line
            for line in lines:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "error" in msg:
                    return msg             # the reply, at last
            if deadline is None:
                deadline = time.monotonic() + DRAIN_S
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break                      # only events came back
            s.settimeout(min(timeout, remaining))
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
