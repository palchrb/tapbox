#!/usr/bin/env python3
"""Storytel on a Sonos: mint the signed url at the last possible moment.

A Sonos cannot play a local path, and a storytel book IS a local path.
So the queue keeps the local path (it is the bookmark and reconcile key)
and _sonos_body swaps in a freshly minted signed CDN url milliseconds
before the SOAP /play — short-lived by design, so late minting is the
whole trick. A book therefore need not be downloaded at all to play in
another room.

Four things QA found that this pins, each of which silently breaks
something:

  - the http-only filter in sonos_start_target drops every storytel row
    (local paths), so nothing ever reaches the speaker;
  - _sonos_body used to be called TWICE per play, which would mint two
    urls and could fail AFTER the speaker is already playing;
  - _sonos_body must NEVER raise: it runs on _sonos_step_worker and
    _sonos_poller, neither guarded, and an escape kills those threads —
    next/prev dead until a restart, or no bookmarks for the session.
    login() raises RuntimeError as well as OSError;
  - after a daemon restart the speaker holds a signed url minted by the
    previous process, so matching the queue on url equality gives idx
    None, which silently disables bookmarks and queue advance."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import storytel, content  # noqa: E402

TARGET = "storytel:series:26175"
BOOKS = [
    {"consumable_id": "111", "title": "En", "order": 1,
     "duration_ms": 721893, "cover": "https://covers.storytel.com/1.jpg"},
    {"consumable_id": "222", "title": "To", "order": 2,
     "duration_ms": 700000, "cover": "https://covers.storytel.com/2.jpg"},
]
storytel.write_shelf(TARGET, "Kokosbananas", BOOKS)
d = storytel.cache_dir(TARGET)
with open(os.path.join(d, "111.mp3"), "wb") as f:   # only book 1 downloaded
    f.write(b"\xff\xfb audio")

# 1. locally, expand is download-only: book 2 is omitted, urls are paths
content.PREFER_REMOTE = False
rows = content.expand_entries(TARGET)
assert [r["id"] for r in rows] == ["111"], rows
assert rows[0]["url"].endswith("111.mp3")
print("1. on the box, only downloaded books, as local paths OK")

# 2. for a sonos renderer EVERY book is listed — the speaker fetches from
#    the CDN, so a book need not be downloaded at all. url stays the LOCAL
#    path: it is the bookmark key, and an http placeholder would leak a
#    signed token into the state file.
content.PREFER_REMOTE = True
rows = content.expand_entries(TARGET)
assert [r["id"] for r in rows] == ["111", "222"], rows
assert all(not r["url"].startswith("http") for r in rows), \
    "the queue url must stay the local path, never an invented http url"
assert rows[1]["art_url"] == "https://covers.storytel.com/2.jpg", \
    "the http cover must ride along — sonos cannot fetch a local jpg"
content.PREFER_REMOTE = False
print("2. for sonos, every book is listed and the url stays local OK")

# --- a scripted orchestrator for the _sonos_body half ----------------------
MINTED = []


def fake_asset(cid, timeout=15):
    MINTED.append((cid, timeout))
    return f"https://fastly-ng.storytel.net/mp3encoder-128/uuid-{cid}?token=T"


storytel.asset_url = fake_asset
daemon._renderer.read = lambda: {"renderer": "sonos", "uid": "RINCON_1"}

orch = object.__new__(daemon.Orchestrator)
orch.target = TARGET
ep = {"url": os.path.join(d, "111.mp3"), "title": "En", "id": "111",
      "image": os.path.join(d, "111.jpg"),
      "art_url": "https://covers.storytel.com/1.jpg"}

# 3. the body carries a freshly minted signed url and the http cover, and
#    asks for it with a SHORT timeout (the default would let a login retry
#    stack to ~70s while the renderer card goes 'unreachable' at 15s)
body = orch._sonos_body(ep, "RINCON_1", 0.0)
assert body["kind"] == "url"
assert body["uri"].startswith("https://fastly-ng.storytel.net/"), body
assert body["art"] == "https://covers.storytel.com/1.jpg", body
assert MINTED == [("111", 6)], f"one mint, short timeout: {MINTED}"
print("3. the body mints one signed url, with an http cover and a short "
      "timeout OK")

# 4. IT MUST NEVER RAISE. Both OSError and RuntimeError (login refused, or
#    inside the refused-login cooldown) become an error dict — a raise here
#    kills _sonos_step_worker (next/prev dead until restart) and
#    _sonos_poller (no bookmarks or queue advance for the session).
for exc in (OSError("offline"), RuntimeError("login refused")):
    def boom(cid, timeout=15, _e=exc):
        raise _e
    storytel.asset_url = boom
    body = orch._sonos_body(ep, "RINCON_1", 0.0)
    assert body.get("error"), f"{type(exc).__name__} must degrade, not raise"
    assert "uri" not in body, "a failed mint must not hand over a stale uri"
storytel.asset_url = fake_asset
print("4. a failed mint degrades to an error dict, never a raise OK")

# 5. _sonos_body is called ONCE per play. Twice meant two authenticated
#    round-trips, two signed urls, and a second failure possible AFTER the
#    speaker was already playing.
src = open(daemon.__file__, encoding="utf-8").read()
i = src.index("def _sonos_play_entry")
j = src.index("def _sonos_start_spotify", i)
assert src.count("self._sonos_body(", i, j) == 1, \
    "_sonos_body must be built once and reused, not called twice"
assert "body = self._sonos_body(" in src[i:j]
print("5. the play path builds the body exactly once OK")

# 6. the http-only filter lets storytel through — without this escape
#    hatch every book, downloaded or not, is dropped before the speaker
assert "_storytel.is_storytel(target)" in src[
    src.index("playable = [e for e in entries"):
    src.index("playable = [e for e in entries") + 400], \
    "storytel needs the same filter exemption the NRK series service has"
print("6. storytel rows survive the http-only playable filter OK")

# 7. the restart reconcile matches on the BOOK ID, not url equality: the
#    speaker holds a signed url minted by the previous daemon process, and
#    a None index silently disables bookmarks and queue advance
k = src.index("ORCH.sonos_idx = next(")
window = src[k - 800:k + 400]
assert 'e["id"] == want' in window and "consumableId" in window, \
    "match the signed url's consumableId QUERY PARAM, not a substring"
# and prove why: a bare substring also hits the 75-char token by luck,
# and next() would then adopt the WRONG book and bookmark onto it
import urllib.parse as _up  # noqa: E402

_uri = ("https://fastly-ng.storytel.net/mp3encoder-128/uuid"
        "?consumableId=331854&isbn=9788&token=aBc331767xyz")
_ids = ["331854", "331855", "331767"]
assert [i for i in _ids if i in _uri] == ["331854", "331767"], \
    "the substring form is genuinely ambiguous"
_want = (_up.parse_qs(_up.urlsplit(_uri).query).get("consumableId")
         or [None])[0]
assert [i for i in _ids if i == _want] == ["331854"]
print("7. the restart reconcile matches a signed url by consumableId OK")

# 8. the expand cache is cleared on every renderer switch: entries differ
#    by renderer, and a stale one makes the box play the WRONG book
assert src.count("_EXPAND_CACHE.clear()") >= 3, \
    "clear the expand cache wherever PREFER_REMOTE flips"
print("8. a renderer switch clears the expand cache OK")

# 9. THE DURATION RIDES ALONG. A Sonos handed a signed url with no file
#    extension reports 0:00 until it has buffered, and the screen draws
#    the times but NO progress bar without a duration (field 2026-08-15:
#    "right seconds, no orange"). We know the length from the shelf, so
#    we send it in the DIDL and seed our own snapshot with it.
content.PREFER_REMOTE = True
rows = content.expand_entries(TARGET)
content.PREFER_REMOTE = False
assert rows[0]["dur_s"], "the shelf's length must reach the queue row"
ep2 = dict(ep, dur_s=721.9)
storytel.asset_url = fake_asset
body = orch._sonos_body(ep2, "RINCON_1", 0.0)
assert body["duration_s"] == 721.9, \
    "the length must ride in the /play body, or Sonos has to guess it"
print("9. the known length is sent to the speaker, not guessed OK")

# 10. and a speaker that reports no usable duration must not erase one we
#     have. The test is FALSY, not `is None`: a Sonos handed a signed url
#     reports TrackDuration "0:00:00", which the sidecar turns into 0 —
#     and an `is None` check let that zero through, so the screen showed
#     a 0:00 duration and drew no bar (field 2026-08-15, the second time).
src_p = src[src.index('if not snap.get("dur_s"):'):][:1400]
assert "kept" in src_p and "dict(snap, dur_s=kept)" in src_p, \
    "a missing/zero duration from the speaker must not overwrite a known one"
assert 'if not snap.get("dur_s"):' in src_p, \
    "the guard must be falsy — the speaker sends 0, not None"
print("10. a speaker's zero duration cannot erase a known one OK")

# 10b. AND IT MUST NOT CARRY ACROSS A TRACK CHANGE. This one destroys
#      data, not pixels: save_state DELETES an episode's bookmark when
#      pos > duration - RESUME_MIN_S ("finished"). A 12-minute
#      Kokosbananas length dragged into an 8-hour Harry Potter wiped the
#      child's place the moment it passed 11:40 — and the rule was only
#      reachable at all because we made duration truthy that same day
#      (field 2026-08-15).
assert 'prev["uri"] == snap.get("uri")' in src_p, \
    "a duration may only be carried forward for the SAME track"
# the url-kind seed (the one storytel uses) is the LAST of the two
seed = src[src.rindex('"dur_s": ep.get("dur_s")'):][:400]
assert '"uri": body.get("uri")' in seed, \
    "the seeded snapshot needs its uri, or the carry-forward can never match"
print("10b. a duration is never carried across a track change OK")

# 10c. and the deletion it guards is real: prove save_state drops the
#      episode when the duration is wrong-and-short, so the guard above
#      is protecting something that actually bites
from vibb import bookmarks as _bm  # noqa: E402

_k = "sonos-dur-probe"
_bm.save_state(_k, "/x/hp1.mp3", 3000.0, "hp1", 28800.0)   # 50min into 8h
assert (_bm.load_state(_k).get("episodes") or {}).get("hp1"), "kept"
_bm.save_state(_k, "/x/hp1.mp3", 3000.0, "hp1", 720.0)     # wrong short dur
assert not (_bm.load_state(_k).get("episodes") or {}).get("hp1"), \
    "a short wrong duration DELETES the bookmark — that is what 10b stops"
print("10c. a wrong short duration really does delete the bookmark OK")

# 11. PAUSE MUST NOT REWIND THE BAR. While PLAYING the position is
#     extrapolated forward by the snapshot's age plus the sidecar's own
#     measurement lag, so the bar keeps up with the Sonos app. Pausing
#     stops that — and falling back to the raw measurement made the bar
#     jump visibly BACKWARDS (5s in the field, 2026-08-15) and forwards
#     again on resume. The audio was always right; the display was not.
o2 = object.__new__(daemon.Orchestrator)
o2.sonos_snap = {"rel_s": 100.0, "dur_s": 700.0, "transport": "PLAYING",
                 "stale_s": 1.0}
o2.sonos_snap_at = daemon.time.monotonic() - 4.0
playing_at = o2._sonos_position()
assert round(playing_at) == 105, playing_at      # 100 + 4s age + 1s lag
frozen = o2._sonos_position()
o2.sonos_snap = dict(o2.sonos_snap, transport="PAUSED_PLAYBACK",
                     rel_s=frozen, stale_s=0)
o2.sonos_snap_at = daemon.time.monotonic()
assert round(o2._sonos_position()) == round(playing_at), \
    "pausing must freeze the shown position, not rewind to the raw one"
assert "frozen = self._sonos_position()" in src, \
    "the pause verb must freeze the extrapolated position"
print("11. pausing freezes the bar instead of rewinding it OK")

# 12. A REFUSED RESUME-SEEK MUST NOT EAT THE BOOKMARK. When the speaker
#     refuses the seek, playback runs from 0 — and the poller then wrote
#     that near-zero position straight over the good one. 52 minutes into
#     a Harry Potter book became 39 seconds (field 2026-08-15, the actual
#     numbers below). The sharelink branch has had this guard for months;
#     the url branch (podcasts AND storytel) never got it, and neither
#     set nor checked the hold.
from vibb import library as _lib  # noqa: E402

BM_T = "storytel:series:113290"
bm_key = _lib.state_key(BM_T)
_bm.save_state(bm_key, "/c/331854.mp3", 3120.0, "331854", 28800.0)

o3 = object.__new__(daemon.Orchestrator)
o3.target = BM_T
o3.sonos_kind = "url"
o3.sonos_idx = 0
o3.sonos_queue = [{"url": "/c/331854.mp3", "id": "331854", "title": "HP1"}]
o3._sonos_bm_last = 0.0
o3.sonos_snap = {"rel_s": 39.0, "dur_s": 28800.0, "transport": "PLAYING",
                 "stale_s": 0, "ours": True}
o3.sonos_snap_at = daemon.time.monotonic()

o3.sonos_bm_hold = ("331854", 3120.0)       # as the refused seek sets it
o3._sonos_bookmark_now(force=True)
kept_pos = _bm.load_state(bm_key)["episodes"]["331854"]["pos"]
assert kept_pos == 3120.0, f"the held bookmark must survive, got {kept_pos}"

o3.sonos_bm_hold = None                     # the unguarded old behaviour
o3._sonos_bm_last = 0.0
o3._sonos_bookmark_now(force=True)
assert _bm.load_state(bm_key)["episodes"]["331854"]["pos"] == 39.0, \
    "without the hold it really is overwritten — that is the bug"

# and playback passing the held point releases it, so normal listening
# still bookmarks
_bm.save_state(bm_key, "/c/331854.mp3", 3120.0, "331854", 28800.0)
o3.sonos_bm_hold = ("331854", 3120.0)
o3.sonos_snap = dict(o3.sonos_snap, rel_s=3200.0)
o3._sonos_bm_last = 0.0
o3._sonos_bookmark_now(force=True)
assert _bm.load_state(bm_key)["episodes"]["331854"]["pos"] == 3200.0, \
    "past the held point the bookmark must move again"
assert o3.sonos_bm_hold is None, "and the hold is released"

# the url play path must SET the hold, not just log that it kept one
assert 'self.sonos_bm_hold = (ep.get("id"), start_s)' in src, \
    "the refused-seek branch must arm the hold, not merely claim to"
print("12. a refused resume-seek holds the bookmark instead of eating it OK")

print("\nSTORYTEL ON SONOS OK — the url is minted at the last moment, the "
      "queue keeps the local key, and a failed mint never kills a thread.")
