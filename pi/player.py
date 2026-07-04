#!/usr/bin/env python3
"""TapBox mpv wrapper with resume.

Usage: player.py <target> <url> [url...]

Runs mpv over the given queue and remembers where playback stopped
(episode + position, polled every 3s over mpv's IPC socket). The next
run with the same <target> rotates the queue to the remembered episode
and seeks to the remembered position — so a BT dropout, Ctrl+C, power
cut or a re-tapped card continues instead of starting over.

State lives in /var/lib/tapbox/state/<key>.json, keyed on the podcast
slug when <target> is an NRK podcast link, else a hash of the target.
State is cleared when the whole queue finishes naturally.
"""

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
ALSA_DEVICE = "alsa/tapbox_bt"
RESUME_MIN_S = 20   # don't bother resuming the first seconds
POLL_S = 3


def log(msg):
    print(f"player: {msg}", file=sys.stderr, flush=True)


def state_key(target):
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return m.group(1)
    return hashlib.sha1(target.encode()).hexdigest()[:12]


def state_path(key):
    return os.path.join(STATE_DIR, f"{key}.json")


def load_state(key):
    try:
        with open(state_path(key)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(key, url, pos):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(key) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"url": url, "pos": pos, "updated": time.time()}, f)
    os.replace(tmp, state_path(key))


def clear_state(key):
    try:
        os.remove(state_path(key))
    except OSError:
        pass


def ipc(sock_path, *command):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(2)
        s.connect(sock_path)
        s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
        return json.loads(s.recv(65536).split(b"\n")[0])


def ipc_get(sock_path, prop):
    resp = ipc(sock_path, "get_property", prop)
    return resp.get("data") if resp.get("error") == "success" else None


def main():
    if len(sys.argv) < 3:
        print("usage: player.py <target> <url> [url...]", file=sys.stderr)
        sys.exit(1)
    target, urls = sys.argv[1], sys.argv[2:]
    key = state_key(target)

    # Resume: rotate the queue to the remembered episode
    start_pos = 0.0
    st = load_state(key)
    if st and st.get("url") in urls and st.get("pos", 0) > RESUME_MIN_S:
        i = urls.index(st["url"])
        urls = urls[i:] + urls[:i]
        start_pos = float(st["pos"])
        log(f"resuming episode {i + 1} at {int(start_pos)}s")

    sock = f"/tmp/tapbox-mpv-{os.getpid()}.sock"
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--really-quiet",
         f"--audio-device={ALSA_DEVICE}", f"--input-ipc-server={sock}"] + urls)
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())

    # Wait for mpv's IPC socket, then seek to the resume position
    for _ in range(100):
        if proc.poll() is not None:
            sys.exit(proc.returncode or 0)
        try:
            if ipc_get(sock, "playback-time") is not None:
                break
        except OSError:
            pass
        time.sleep(0.2)
    if start_pos:
        try:
            ipc(sock, "seek", start_pos, "absolute")
        except OSError:
            log("could not seek to resume position — playing from start")

    # Poll position and persist it until mpv exits
    while proc.poll() is None:
        try:
            path = ipc_get(sock, "path")
            pos = ipc_get(sock, "playback-time")
            if path and isinstance(pos, (int, float)):
                save_state(key, path, pos)
        except OSError:
            pass
        time.sleep(POLL_S)

    if proc.returncode == 0:
        clear_state(key)  # whole queue finished — next tap starts fresh
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    main()
