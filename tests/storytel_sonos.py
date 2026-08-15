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
src_p = src[src.index('if not snap.get("dur_s"):'):][:2200]
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
o3._seek_at = -1e9
o3.sonos_snap = {"rel_s": 39.0, "dur_s": 28800.0, "transport": "PLAYING",
                 "stale_s": 0, "ours": True}
o3.sonos_snap_at = daemon.time.monotonic()

o3.sonos_bm_hold = ("331854", 3120.0, daemon.time.monotonic())
o3._sonos_bookmark_now(force=True)
kept_pos = _bm.load_state(bm_key)["episodes"]["331854"]["pos"]
assert kept_pos == 3120.0, f"the held bookmark must survive, got {kept_pos}"

# and with the hold gone the REGRESSION guard still catches it — belt
# and braces, because the hold only knows about a refused seek
o3.sonos_bm_hold = None
o3._sonos_bm_last = 0.0
o3._sonos_bookmark_now(force=True)
assert _bm.load_state(bm_key)["episodes"]["331854"]["pos"] == 3120.0, \
    "the regression guard must catch what the hold does not"

# and playback passing the held point releases it, so normal listening
# still bookmarks
_bm.save_state(bm_key, "/c/331854.mp3", 3120.0, "331854", 28800.0)
o3.sonos_bm_hold = ("331854", 3120.0, daemon.time.monotonic())
o3.sonos_snap = dict(o3.sonos_snap, rel_s=3200.0)
o3._sonos_bm_last = 0.0
o3._sonos_bookmark_now(force=True)
assert _bm.load_state(bm_key)["episodes"]["331854"]["pos"] == 3200.0, \
    "past the held point the bookmark must move again"
assert o3.sonos_bm_hold is None, "and the hold is released"

# the url play path must SET the hold, not just log that it kept one
assert 'self.sonos_bm_hold = (ep.get("id"), start_s,' in src, \
    "the refused-seek branch must arm the hold, not merely claim to"
assert "BM_HOLD_MAX_S" in src, \
    "and the hold must be BOUNDED — unbounded it silences the writes " \
    "that matter, which loses the place just as surely"
print("12. a refused resume-seek holds the bookmark instead of eating it OK")

# 13. ADOPTING A LIVE SESSION MUST RESTORE THE LENGTH. The snapshot the
#     reconcile adopts comes straight from the speaker, which reports 0
#     for a signed url — and a fresh process has no earlier value to
#     carry forward. So a daemon restart mid-book left the card with
#     duration 0 and no position at all (field 2026-08-15: 33666s before
#     the restart, 0 after). The queue row knows it, from the shelf.
adopt = src[src.index("# Restore the LENGTH from the queue row."):][:900]
assert 'not (ORCH.sonos_snap or {}).get("dur_s")' in adopt, \
    "restore only when the adopted snapshot has no usable length"
assert 'ORCH.sonos_queue[ORCH.sonos_idx].get(' in adopt and "dur_s" in adopt, \
    "the length must come from the matched queue row"
print("13. adopting a live session restores the book's length OK")

# 14. AND THE LENGTH IS LOOKED UP, NOT INHERITED. Carrying the last
#     known duration forward only works when there IS one — so the bar
#     was right after vibb started a book and wrong after a restart or
#     when playback was started FROM THE SPEAKER (field 2026-08-15:
#     "right sometimes, not always"). The shelf knows every book's
#     length; look it up by the consumableId the signed url carries.
o4 = object.__new__(daemon.Orchestrator)
o4.target = "storytel:series:113290"
o4.sonos_queue = [{"id": "331854", "dur_s": 33666.0},
                  {"id": "331855", "dur_s": 30000.0}]
# the url names book 2, and book 1's id also appears inside the token
u = ("https://fastly-ng.storytel.net/mp3encoder-128/uuid"
     "?consumableId=331855&token=abc331854xyz")
assert o4._sonos_known_duration(u) == 30000.0, \
    "must key on the consumableId param, not a substring of the token"
assert o4._sonos_known_duration("https://x/y?consumableId=999") is None
assert o4._sonos_known_duration(None) is None
o4.target = "https://radio.nrk.no/podkast/x"
assert o4._sonos_known_duration(u) is None, "scoped to storytel targets"
# and the poller prefers the lookup over the carry-forward
poll = src[src.index("# Authoritative first:"):][:700]
assert "_sonos_known_duration(snap.get(\"uri\"))" in poll and "else:" in poll, \
    "look the length up first, fall back to carrying forward"
print("14. the length is looked up per book, not inherited OK")

# 15. A BOOKMARK MAY NOT FALL BACKWARDS ON ITS OWN. Belt and braces over
#     the refused-seek hold, which only covers the one cause we know
#     about. Every other way a speaker can report a near-zero position —
#     a session it forgot, a restart that re-queued from the top, a
#     re-opened track — destroyed the child's place, repeatedly (field
#     2026-08-15, three separate reports). Playback only moves forward,
#     so a large drop is a fault; the legitimate ways back announce
#     themselves (a /seek stamps _seek_at, an episode start arms the
#     hold).
def bm_orch(rel, seek_at=-1e9):
    o = object.__new__(daemon.Orchestrator)
    o.target = BM_T
    o.sonos_kind = "url"
    o.sonos_idx = 0
    o._sonos_bm_last = 0.0
    o.sonos_bm_hold = None
    o._seek_at = seek_at
    o.sonos_queue = [{"url": "/c/331854.mp3", "id": "331854"}]
    o.sonos_snap = {"rel_s": rel, "dur_s": 33666.0, "transport": "PLAYING",
                    "stale_s": 0, "ours": True}
    o.sonos_snap_at = daemon.time.monotonic()
    return o


def bm_run(prev, rel, seeked=False):
    _bm.save_state(bm_key, "/c/331854.mp3", prev, "331854", 33666.0)
    o = bm_orch(rel, seek_at=daemon.time.monotonic() if seeked else -1e9)
    o._sonos_bookmark_now()
    return _bm.load_state(bm_key)["episodes"]["331854"]["pos"]


# the bug: the speaker restarted the track and nothing asked for it
assert bm_run(3120.0, 5.0) == 3120.0, "a speaker restart must not win"
# ...but every legitimate way backwards MUST still write. Keying on the
# SIZE of the drop would have fought all three of these — the seek card
# alone steps up to five minutes — which is why the guard keys on
# landing at the TOP instead.
assert bm_run(3120.0, 2400.0) == 2400.0, "a 12-minute manual step back"
assert bm_run(10800.0, 9000.0) == 9000.0, "a 30-minute seek from hour 3"
assert bm_run(3120.0, 5.0, seeked=True) == 5.0, "a deliberate seek to the top"
assert bm_run(3120.0, 3200.0) == 3200.0, "ordinary forward listening"
assert bm_run(200.0, 5.0) == 5.0, "a restart with little to lose"
print("15. a track restart cannot eat the bookmark, and real seeks still write OK")

# 16. PLAY AFTER A RESTART MUST RESUME, NOT RESTART. With no live
#     session of ours, playpause used to send a bare /resume to a
#     speaker that no longer had our queue — so it played from the top.
#     That is why pressing play right after a daemon restart started an
#     audiobook over, while going out and back in (a real /play, which
#     reads the bookmark) resumed correctly (field 2026-08-15).
pp = []
o5 = object.__new__(daemon.Orchestrator)
o5.target = BM_T
o5.sonos_snap, o5.sonos_snap_at = {}, 0.0          # stale / no session
o5.sonos_start_target = lambda t, episode=None: pp.append(("start", t)) or {}
daemon._renderer.read = lambda: {"renderer": "sonos", "uid": "U"}
daemon._renderer.post = lambda p, b=None, **k: pp.append(("post", p)) or (200, {})
o5._sonos_command("playpause")
assert pp == [("start", BM_T)], f"must start from the bookmark, got {pp}"

pp.clear()
o5.sonos_snap = {"ours": True, "transport": "PAUSED_PLAYBACK", "rel_s": 10.0}
o5.sonos_snap_at = daemon.time.monotonic()
o5._sonos_command("playpause")
assert pp == [("post", "/resume")], f"a live session still just resumes: {pp}"
print("16. play with no live session resumes from the bookmark OK")

# 17. A SEEK MUST NOT SNAP BACK. The seek patches the snapshot
#     optimistically, but the poller replaces it wholesale every few
#     seconds and the speaker needs a moment to actually get there — so
#     the bar jumped to the target, snapped back to the old spot, then
#     jumped forward again: the box and the speaker visibly disagreeing
#     mid-seek (field 2026-08-15). Held until the speaker lands near it,
#     exactly like the transport flip already is.
assert "sonos_opt_pos" in src, "a seek needs an optimistic position hold"
hold = src[src.index("optp = ORCH.sonos_opt_pos"):][:600]
assert "SONOS_SEEK_TOL_S" in hold and "SONOS_SEEK_HOLD_S" in hold, \
    "release the hold when the speaker lands near it, or the window passes"
assert "self.sonos_opt_pos = (tgt, time.monotonic()," in src, \
    "ORCH.seek must arm the hold"
assert "self.sonos_snap = dict(self.sonos_snap, rel_s=tgt" not in src, \
    "and must NOT write that guess into the bookmark's source snapshot"
print("17. a sonos seek holds its position instead of snapping back OK")

# 18. THE RENDERER IS KNOWN AT PLAY TIME — do not depend on a global a
#     DIFFERENT THREAD sets at startup. The poller flips PREFER_REMOTE,
#     so a play issued before that thread ran expanded to the downloaded
#     books only; a streamed book was missing from the queue, the
#     bookmark's episode id matched nothing, idx fell back to 0, and the
#     series restarted at book one from zero — bookmarking zero on the
#     way (field 2026-08-15: restart vibbd under sonos, play, place gone).
sst = src[src.index("def sonos_start_target"):][:1400]
assert "content.PREFER_REMOTE = True" in sst, \
    "sonos_start_target must set PREFER_REMOTE itself, not inherit a race"
assert sst.index("content.PREFER_REMOTE = True") < \
    sst.index("entries = content.expand_entries(target)"), \
    "and it must be set BEFORE the expansion it governs"
print("18. the sonos play path expands remote-aware without a race OK")

# 19. THE SHELF LENGTH MAY NOT DELETE A BOOKMARK. save_state drops an
#     episode when pos > duration - RESUME_MIN_S, and our duration can
#     be Storytel's number rather than the speaker's. If it is short by
#     half a minute against the real file, a ten-hour book loses its
#     place near the end. Good enough to draw a bar, not to delete data.
assert 'episode_id=ep.get("id"), duration=None)' in src, \
    "the sonos bookmark write must not pass a shelf-derived duration"
print("19. the shelf length cannot delete a bookmark OK")

# 20. and the poller tick is guarded WHOLESALE: a ValueError from a bad
#     url or an OSError from a full SD card inside the bookmark write
#     used to kill the thread, taking snapshots, bookmarks and queue
#     advance with it for the rest of the session.
assert "sonos poll tick failed" in src, \
    "one guard around the whole tick, not point defences"
print("20. a raising poller tick cannot kill the thread OK")

print("\nSTORYTEL ON SONOS OK — the url is minted at the last moment, the "
      "queue keeps the local key, and a failed mint never kills a thread.")
