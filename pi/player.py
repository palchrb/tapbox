#!/usr/bin/env python3
"""TapBox player — THE entrypoint for playing any link.

Usage: player.py [--fresh] [--episode <id>] <target> [url...]

Routing:
  - Spotify links/URIs (track/album/playlist/artist/episode/show, incl.
    spotify.link short links) -> go-librespot's HTTP API
  - everything else (NRK podcast/serie, RSS feeds, streams, local files)
    -> expanded via nrk.py and played with mpv, with resume: position is
    polled over mpv's IPC socket and the next run of the same target
    continues where it stopped. A background episode sync is kicked off
    for NRK podcasts.

So this is the pure-python way to play anything:

    sudo python3 player.py "https://open.spotify.com/track/..."
    sudo python3 player.py "https://radio.nrk.no/podkast/<slug>"
    sudo python3 player.py --fresh "<link>"     # ignore remembered position

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
import urllib.request

STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
API = "http://127.0.0.1:3678"
RESUME_MIN_S = 20   # don't bother resuming the first seconds
POLL_S = 3

SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]+$")
SPOTIFY_LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?"
    r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)")


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


def save_state(key, url, pos, episode_id=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(key) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"url": url, "pos": pos, "id": episode_id,
                   "updated": time.time()}, f)
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
        # mpv interleaves async events with command replies; find the line
        # that actually answers our command (has an "error" field).
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


def ipc_get(sock_path, prop):
    try:
        resp = ipc(sock_path, "get_property", prop)
    except (OSError, ValueError):
        return None
    return resp.get("data") if resp.get("error") == "success" else None


def api(path, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_spotify(target):
    return (target.startswith("spotify:") or "open.spotify.com" in target
            or "spotify.link/" in target)


def output_pcm():
    """The ALSA pcm playback goes to — set via tapboxd POST /output
    (bt = the paired speaker, local = the built-in/HAT speaker)."""
    try:
        with open(os.path.join(STATE_DIR, "output.json")) as f:
            return json.load(f)["pcm"]
    except (OSError, ValueError, KeyError):
        return "tapbox_bt"


def online():
    """Quick connectivity probe. TAPBOX_OFFLINE=1 forces offline mode
    (manual travel switch / tests). Plain IP:port — no DNS to hang on."""
    if os.environ.get("TAPBOX_OFFLINE"):
        return False
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        return True
    except OSError:
        return False


def to_spotify_uri(target):
    if SPOTIFY_URI_RE.match(target):
        return target
    if "spotify.link/" in target:  # short links redirect to open.spotify.com
        with urllib.request.urlopen(target, timeout=10) as r:
            target = r.url
    m = SPOTIFY_LINK_RE.search(target)
    return f"spotify:{m.group(1)}:{m.group(2)}" if m else None


def play_spotify(target):
    uri = to_spotify_uri(target)
    if not uri:
        log(f"could not parse spotify link: {target}")
        sys.exit(1)
    try:
        api("/player/play", {"uri": uri})
    except OSError as e:
        log(f"go-librespot API unreachable ({e}) — check: journalctl -u go-librespot")
        sys.exit(1)
    log(f"spotify: playing {uri}")
    time.sleep(2)
    try:
        track = (json.loads(api("/status")) or {}).get("track") or {}
        if track.get("name"):
            artists = ", ".join(track.get("artist_names") or [])
            log(f"now playing: {track['name']} — {artists}")
    except (OSError, ValueError):
        pass


def main():
    args = sys.argv[1:]
    fresh = False
    reverse = False  # flip the expanded queue (library 'order' override)
    episode = None   # explicit episode pick from the menu (tapboxd /play)
    while args and args[0] in ("--fresh", "--reverse", "--episode"):
        if args[0] == "--fresh":
            fresh = True
            args = args[1:]
        elif args[0] == "--reverse":
            reverse = True
            args = args[1:]
        else:
            if len(args) < 2:
                print("--episode needs an id", file=sys.stderr)
                sys.exit(1)
            episode = args[1]
            args = args[2:]
    if not args:
        print("usage: player.py [--fresh] [--reverse] [--episode <id>] "
              "<target> [url...]", file=sys.stderr)
        sys.exit(1)
    target, urls = args[0], args[1:]

    if is_spotify(target):
        play_spotify(target)  # resume is Spotify's own job — session remembers
        return

    titles, ids, images = {}, {}, {}
    if not urls:  # expand the link ourselves — pure-python entrypoint
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            import nrk
            entries = nrk.expand_entries(target)
            urls = [e["url"] for e in entries]
            titles = {e["url"]: e["title"] for e in entries if e.get("title")}
            ids = {e["url"]: e["id"] for e in entries if e.get("id")}
            images = {e["url"]: e["image"] for e in entries if e.get("image")}
        except Exception as e:
            log(f"expansion failed ({e!r}) — playing the raw link")
            urls = [target]
    if reverse and len(urls) > 1:
        urls.reverse()  # titles/ids are url-keyed dicts — unaffected
        log("queue order reversed (library setting)")

    # Offline? Don't let mpv grind through dead stream URLs — play what is
    # on disk. (All-remote queues are left alone: failing is the only option.)
    streams = [u for u in urls if u.startswith(("http://", "https://"))]
    if streams and len(streams) < len(urls) and not online():
        urls = [u for u in urls if not u.startswith(("http://", "https://"))]
        log(f"offline — playing {len(urls)} cached episode(s), "
            f"skipping {len(streams)} streams")
    key = state_key(target)
    if fresh:
        clear_state(key)
        log("starting fresh — cleared remembered position")

    try:
        api("/player/pause", {})  # don't talk over Spotify
    except OSError:
        pass

    # Queue planning. An explicit --episode (picked in a menu) wins over the
    # bookmark; otherwise resume: rotate the queue to the remembered episode.
    # Matching is on the stable episode id first — the same episode can be a
    # stream URL one run and a cached local file the next, so URLs alone are
    # not reliable.
    start_pos = 0.0
    st = load_state(key)
    url_by_id = {eid: u for u, eid in ids.items()}
    if episode:
        picked = url_by_id.get(episode)
        if picked is not None and picked not in urls:
            picked = None  # filtered away (offline) — fall back gracefully
        if picked is None:
            log(f"episode '{episode}' not in this queue — playing from start")
        else:
            idx = urls.index(picked)
            urls = urls[idx:] + urls[:idx]
            # Picking the bookmarked episode continues at its position;
            # picking any other episode starts it from the top. The bookmark
            # follows playback from here on, as always.
            if st and st.get("id") == episode and st.get("pos", 0) > RESUME_MIN_S:
                start_pos = float(st["pos"])
            name = titles.get(picked) or episode
            log(f"starting at '{name}'"
                + (f", {int(start_pos)}s" if start_pos else ""))
    elif st and st.get("pos", 0) > RESUME_MIN_S:
        idx = None
        if st.get("id") and url_by_id.get(st["id"]) in urls:
            idx = urls.index(url_by_id[st["id"]])
        if idx is None and st.get("url") in urls:
            idx = urls.index(st["url"])
        if idx is not None:
            urls = urls[idx:] + urls[:idx]
            start_pos = float(st["pos"])
            name = titles.get(urls[0]) or f"episode {idx + 1}"
            log(f"resuming '{name}' at {int(start_pos)}s")

    # Fixed socket path so the button daemon (tapbox-buttons) can find us
    sock_dir = "/run" if os.access("/run", os.W_OK) else "/tmp"
    sock = os.environ.get("TAPBOX_MPV_SOCK",
                          os.path.join(sock_dir, "tapbox-mpv.sock"))
    try:
        os.remove(sock)
    except OSError:
        pass
    # Start at the box volume last set through tapboxd (POST /volume)
    try:
        with open(os.path.join(STATE_DIR, "volume.json")) as f:
            volume = max(0, min(100, round(json.load(f)["volume"])))
    except (OSError, ValueError, KeyError, TypeError):
        volume = 100
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--really-quiet",
         # A2DP/SBC to the speaker runs at 44100 Hz; low-bitrate audiobooks
         # come in at 16000 Hz which the BT link can't deliver (silence).
         # Resample everything to 44100 stereo so any source rate plays.
         "--audio-samplerate=44100", "--audio-channels=stereo",
         f"--volume={volume}",
         f"--audio-device=alsa/{output_pcm()}",
         f"--input-ipc-server={sock}"] + urls)
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())

    # NRK podcast/series? Cache the newest episodes in the background
    kind = None
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        kind = "podcast"
    else:
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            kind = "series"
    if m:
        nrkpy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nrk.py")
        if os.path.exists(nrkpy):
            subprocess.Popen([sys.executable, nrkpy, "sync", m.group(1), "50", kind],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             preexec_fn=lambda: os.nice(19))  # never compete with audio
            log(f"background sync started for '{m.group(1)}' ({kind})")

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

    # Poll position and persist it until mpv exits; log track changes
    os.makedirs(STATE_DIR, exist_ok=True)
    now_file = os.path.join(STATE_DIR, "now-playing.json")
    last_np = None
    last_title = None
    last_beat = 0.0
    while proc.poll() is None:
        try:
            path = ipc_get(sock, "path")
            # Publish which episode is playing (tapboxd /status -> menu
            # highlight); written only when the track changes.
            if path and path != last_np:
                last_np = path
                try:
                    with open(now_file + ".tmp", "w") as f:
                        json.dump({"id": ids.get(path), "url": path,
                                   "title": titles.get(path),
                                   "image": images.get(path),
                                   "target": target}, f)
                    os.replace(now_file + ".tmp", now_file)
                except OSError:
                    pass
            pos = ipc_get(sock, "playback-time")
            # A live stream (radio) has no finite duration — don't bookmark
            # it (its "position" is the live-edge timestamp, not progress).
            live = ipc_get(sock, "duration") in (None, 0)
            if path and isinstance(pos, (int, float)):
                if not live:
                    save_state(key, path, pos, ids.get(path))
                # heartbeat so a quiet-but-playing stream isn't mistaken
                # for frozen (mpv runs silent); every ~30s
                now_m = time.monotonic()
                if now_m - last_beat > 30:
                    last_beat = now_m
                    log("...playing (live)" if live else f"...playing, {int(pos)}s")
            # Prefer the catalog title (NRK mp3s lack ID3, so mpv's
            # media-title falls back to an unhelpful filename)
            title = (titles.get(path) if path else None) or ipc_get(sock, "media-title")
            if title and title != last_title:
                last_title = title
                log(f"now playing: {title}")
        except OSError:
            pass
        time.sleep(POLL_S)

    if proc.returncode == 0:
        clear_state(key)  # whole queue finished — next tap starts fresh
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)
