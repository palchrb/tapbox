"""Playback bookmarks — lifted out of player.py so a REMOTE renderer can
write them too.

The functions are byte-identical to their player.py originals. The move
exists because the Sonos renderer has no player process at all: tapboxd's
poller must persist positions itself, and keeping a headless player.py
alive just to reuse save_state would make _mpv_alive() lie to the six
decisions that consult it (stall watchdog, _audible_now, _streaming_now,
command rule 1, status gate, boot-resume guard — architect review
2026-08-08). player.py re-exports these names, so its callers and the
existing tests are untouched.

File format is UNCHANGED: {url, pos, id, updated, episodes:{key:{pos,
url, updated}}} per state_key, in STATE_DIR. Anything that changes here
changes what a yanked battery loses — see tests/episode_resume.py.
"""

import json
import os
import time

from tapbox.paths import STATE_DIR

RESUME_MIN_S = 20   # don't bother resuming the first seconds


def state_path(key):
    return os.path.join(STATE_DIR, f"{key}.json")


def load_state(key):
    try:
        with open(state_path(key)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(key, url, pos, episode_id=None, duration=None):
    """Persist playback position. The top-level {url,pos,id} is the
    whole-feed bookmark (which episode was last playing, for resume-on-tap);
    `episodes` additionally remembers a position PER episode so hopping
    between episodes continues each where it was left. Keyed by the stable
    episode id (falls back to url), so a stream and its cached file share
    one slot. An episode played to its end is dropped from the map — a
    re-tap then starts it fresh instead of at the last second."""
    os.makedirs(STATE_DIR, exist_ok=True)
    st = load_state(key) or {}
    eps = st.get("episodes")
    if not isinstance(eps, dict):
        eps = {}
    ep_key = episode_id or url
    if duration and pos > duration - RESUME_MIN_S:
        eps.pop(ep_key, None)  # finished — no mid-episode resume to keep
    else:
        eps[ep_key] = {"pos": pos, "url": url, "updated": time.time()}
    st.update({"url": url, "pos": pos, "id": episode_id,
               "updated": time.time(), "episodes": eps})
    tmp = state_path(key) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, state_path(key))


def episode_pos(st, episode_id, url):
    """The remembered position for one specific episode, across its stream
    and cached-file URLs. 0 when unknown."""
    if not st:
        return 0.0
    eps = st.get("episodes")
    if isinstance(eps, dict):  # new format: the map is authoritative — an
        rec = eps.get(episode_id) if episode_id is not None else None  # episode
        rec = rec or eps.get(url) or {}                       # cleared on finish
        return float(rec.get("pos") or 0)                     # must stay cleared
    # back-compat: state files written before per-episode memory only had
    # the single top-level bookmark
    if episode_id is not None and st.get("id") == episode_id:
        return float(st.get("pos") or 0)
    return 0.0


def clear_state(key):
    try:
        os.remove(state_path(key))
    except OSError:
        pass


def rotate_to_bookmark(urls, st, url_by_id):
    """(queue, start_pos) for a whole-feed bookmark: rotate the queue so
    the bookmarked EPISODE always comes first — finishing episode N rolls
    the bookmark onto N+1 at ~0s, and skipping the rotation for an early
    position sent the replay back to the queue top ('why is it playing
    episode 1 again?'). Only the SEEK is gated by RESUME_MIN_S."""
    idx = None
    if st.get("id") and url_by_id.get(st["id"]) in urls:
        idx = urls.index(url_by_id[st["id"]])
    if idx is None and st.get("url") in urls:
        idx = urls.index(st["url"])
    if idx is None:
        return urls, 0.0
    pos = float(st.get("pos") or 0)
    return urls[idx:] + urls[:idx], (pos if pos > RESUME_MIN_S else 0.0)
