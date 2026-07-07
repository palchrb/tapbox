#!/usr/bin/env python3
"""TapBox player — THE entrypoint for playing any link.

Usage: player.py [--fresh] [--reverse] [--episode <id>] [--cache <n>]
                 <target> [url...]

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

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from tapbox import content, mpv as _mpv, spotify  # noqa: E402
from tapbox.output import audio_ready  # noqa: E402
from tapbox.paths import STATE_DIR  # noqa: E402

is_spotify = spotify.is_spotify
RESUME_MIN_S = 20   # don't bother resuming the first seconds
POLL_S = 3


def log(msg):
    print(f"player: {msg}", file=sys.stderr, flush=True)


from tapbox.library import state_key  # noqa: E402  (shared with tapboxd)


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
    return _mpv.ipc(list(command), sock=sock_path)


def ipc_get(sock_path, prop):
    try:
        return _mpv.get(prop, sock=sock_path)
    except (OSError, ValueError):
        return None


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


SPOT_BM_FILE = os.path.join(STATE_DIR, "spotify-bookmark.json")
SPOT_RESUME_MIN_MS = 20000


def _apply_box_volume():
    """One volume knob across sources: mpv reads volume.json at every
    start — give go-librespot the same treatment, else it keeps whatever
    its previous session used and Spotify plays louder/softer than NRK."""
    try:
        with open(os.path.join(STATE_DIR, "volume.json")) as f:
            v = max(0, min(100, round(json.load(f)["volume"])))
    except (OSError, ValueError, KeyError, TypeError):
        return  # never set through the box yet — leave as is
    try:
        steps = spotify.status().get("volume_steps") or 65535
        spotify.go("/player/volume", body={"volume": round(v * steps / 100)})
        log(f"volume set to {v} (box level)")
    except OSError:
        pass


def play_spotify(target, fresh=False):
    uri = spotify.to_uri(target)
    if not uri:
        log(f"could not parse spotify link: {target}")
        sys.exit(1)

    # Exact resume: tapboxd bookkeeps track+position while Spotify plays
    # (its cloud only resumes for Spotify's own clients). Same context ->
    # play {uri, skip_to_uri} keeps the queue intact, then seek.
    bm = None
    if fresh:
        try:
            os.remove(SPOT_BM_FILE)
        except OSError:
            pass
    else:
        try:
            with open(SPOT_BM_FILE) as f:
                bm = json.load(f)
        except (OSError, ValueError):
            log("no spotify bookmark on disk — starting from the top")
            bm = None
        if bm is not None:
            # say WHY a bookmark is rejected — invaluable when 'it started
            # over' reports come in from the field
            if bm.get("context_uri") != uri:
                log(f"bookmark is for another context "
                    f"({bm.get('context_uri')!r} != {uri!r}) — clean start")
                bm = None
            elif not bm.get("uri"):
                log("bookmark has no track uri — clean start")
                bm = None
            elif (bm.get("position") or 0) <= SPOT_RESUME_MIN_MS:
                log(f"bookmark position {int((bm.get('position') or 0) / 1000)}s"
                    f" is below the resume threshold — clean start")
                bm = None

    body = {"uri": uri}
    if bm:
        body["skip_to_uri"] = bm["uri"]
        # Load silently and unpause only after the seek — otherwise the
        # first 1-2s of the track play audibly from 0:00 while we wait
        # for it to load.
        body["paused"] = True
    try:
        spotify.go("/player/play", timeout=10, body=body)
    except OSError as e:
        log(f"go-librespot API unreachable ({e}) — check: journalctl -u go-librespot")
        sys.exit(1)
    log(f"spotify: playing {uri}"
        + (f" (resuming {bm['uri']} at {bm['position'] // 1000}s)" if bm else ""))
    _apply_box_volume()

    if bm:
        # seek once the right track has actually loaded
        for _ in range(12):
            time.sleep(0.5)
            track = spotify.status().get("track") or {}
            if track.get("uri") == bm["uri"]:
                try:
                    spotify.go("/player/seek",
                               body={"position": int(bm["position"])})
                except OSError:
                    log("seek failed — continuing from the track start")
                break
        else:
            log("resume track never loaded — playing the context from start")
        try:
            spotify.go("/player/resume")
        except OSError:
            log("resume call failed — press play to start audio")

    time.sleep(2)
    track = spotify.status().get("track") or {}
    if track.get("name"):
        artists = ", ".join(track.get("artist_names") or [])
        log(f"now playing: {track['name']} — {artists}")


def main():
    args = sys.argv[1:]
    fresh = False
    reverse = False  # flip the expanded queue (library 'order' override)
    episode = None   # explicit episode pick from the menu (tapboxd /play)
    cache_n = None   # library entry cache setting; None = legacy behaviour
    while args and args[0] in ("--fresh", "--reverse", "--episode", "--cache"):
        if args[0] == "--fresh":
            fresh = True
            args = args[1:]
        elif args[0] == "--reverse":
            reverse = True
            args = args[1:]
        elif args[0] == "--cache":
            if len(args) < 2 or not args[1].isdigit():
                print("--cache needs a number", file=sys.stderr)
                sys.exit(1)
            cache_n = int(args[1])
            args = args[2:]
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
        play_spotify(target, fresh=fresh)
        return

    titles, ids, images = {}, {}, {}
    if not urls:  # expand the link ourselves — pure-python entrypoint
        try:
            entries = content.expand_entries(target)
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
        spotify.go("/player/pause")  # don't talk over Spotify
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
    terminated = []  # set when WE are told to stop (reboot/daemon restart)

    def _stop(*_args):
        terminated.append(True)
        proc.terminate()
    signal.signal(signal.SIGTERM, _stop)

    # Background episode caching. A library entry's cache setting (--cache N,
    # passed by tapboxd) decides: 0 = never sync, N = keep the newest N.
    # Without the flag (cards with raw links, CLI) the legacy behaviour
    # stands: NRK podcasts/series sync their newest 50.
    kind = None
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        kind = "podcast"
    else:
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            kind = "series"
    sync_args = None
    if m and (cache_n is None or cache_n > 0):
        sync_args = ["sync", m.group(1), str(cache_n or 50), kind]
    elif (cache_n or 0) > 0 and len(urls) > 1 \
            and target.startswith(("http://", "https://")):
        sync_args = ["sync-feed", target, str(cache_n)]
    if sync_args:
        # content.py is stdlib-only and runs fine as a plain script
        subprocess.Popen([sys.executable, content.__file__, *sync_args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         preexec_fn=lambda: os.nice(19))  # never compete with audio
        log(f"background sync started: {' '.join(sync_args)}")

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

    def survive_dead_audio(stable):
        """When the audio output dies mid-play (BT chip crash, speaker
        powered off), mpv burns through the queue silently — every file
        "ends" within seconds. Pause, roll back to the last episode that
        was actually audible, and wait for the output to come back."""
        log("tracks are flying past with no audio — output looks dead; "
            "pausing")
        try:
            ipc(sock, "set_property", "pause", True)
        except OSError:
            return
        if stable and stable[0] in urls:
            spath, spos = stable
            try:
                ipc(sock, "playlist-play-index", urls.index(spath))
                for _ in range(50):  # wait for the file to load
                    if ipc_get(sock, "path") == spath \
                            and ipc_get(sock, "duration"):
                        break
                    time.sleep(0.2)
                ipc(sock, "set_property", "pause", True)
                ipc(sock, "seek", int(spos), "absolute")
                log(f"rolled back to the last audible episode at "
                    f"{int(spos)}s")
            except OSError:
                pass
        for _ in range(120):  # give the output <=10 min to return
            if audio_ready():
                time.sleep(2)  # let the transport settle
                try:
                    ipc(sock, "set_property", "pause", False)
                except OSError:
                    pass
                log("audio output is back — resuming")
                return
            time.sleep(5)
        log("audio output did not come back — staying paused "
            "(position saved; any play command resumes)")

    # Poll position and persist it until mpv exits; log track changes
    os.makedirs(STATE_DIR, exist_ok=True)
    now_file = os.path.join(STATE_DIR, "now-playing.json")
    last_np = None
    last_title = None
    last_beat = 0.0
    prev_path, track_started = None, time.monotonic()
    fast_skips, stable = 0, None
    while proc.poll() is None:
        try:
            path = ipc_get(sock, "path")
            paused = ipc_get(sock, "pause")
            now_m = time.monotonic()
            if path and path != prev_path:
                if prev_path is not None and not paused:
                    fast_skips = fast_skips + 1 \
                        if now_m - track_started < 10 else 0
                prev_path, track_started = path, now_m
                if fast_skips >= 3:
                    survive_dead_audio(stable)
                    fast_skips, prev_path = 0, None
                    track_started = time.monotonic()
                    continue
            # Publish which episode is playing + the pause state (tapboxd
            # reads the FILE at shutdown — IPC would race mpv's death);
            # written when the track or pause state changes.
            if path and (path, paused) != last_np:
                last_np = (path, paused)
                try:
                    with open(now_file + ".tmp", "w") as f:
                        json.dump({"id": ids.get(path), "url": path,
                                   "title": titles.get(path),
                                   "image": images.get(path),
                                   "paused": bool(paused),
                                   "duration": ipc_get(sock, "duration"),
                                   "target": target}, f)
                    os.replace(now_file + ".tmp", now_file)
                except OSError:
                    pass
            pos = ipc_get(sock, "playback-time")
            # A live stream (radio) has no finite duration — don't bookmark
            # it (its "position" is the live-edge timestamp, not progress).
            live = ipc_get(sock, "duration") in (None, 0)
            if path and isinstance(pos, (int, float)):
                if not paused and now_m - track_started > 15:
                    stable = (path, pos)  # last spot that audibly played
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

    # Clear the bookmark ONLY when the queue truly finished by itself.
    # mpv exits 0 on a clean SIGTERM quit too (reboot, daemon restart,
    # /stop) — clearing there wiped the resume position, so "restart the
    # box" looked like "the audiobook is over".
    if proc.returncode == 0 and not terminated:
        clear_state(key)  # whole queue finished — next tap starts fresh
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)
