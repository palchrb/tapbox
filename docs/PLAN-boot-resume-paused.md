# PLAN: boot lands PAUSED; BT-reconnect mid-story still auto-resumes

Owner request (2026-08-18): the box must NOT start audio on its own after a
reboot, regardless of output — it should land on the now-playing screen for
what was in progress, PAUSED, so one tap continues. BUT when the box repairs
a BROKEN BT CONNECTION mid-session it should resume playing immediately.

Both reviewed by QA (two passes) + verified independently in code. The two
behaviours are ALREADY served by two independent paths; this change only
removes the boot auto-play and leaves the mid-session reconnect path
untouched. Rated low-risk, but it is the box's most sensitive code — do it
with a clear head, run the full suite, and field-test.

## Why it's safe (the load-bearing finding)

Three separate things can start playback (`daemon.py:812-814` comment):
1. `_boot_resume` — runs once at daemon start (the reboot case)
2. A-press / tap replay — `play()`
3. transport-up "blip" resume — BT speaker returned mid-session

Case 2 (owner's second rule) is path 3: `_bt_transport_lost` (POST /bt/lost
from btwatchd, `daemon.py:4712`) arms `_BT_WAIT["lost"]`; `_bt_wait_watcher`
(`:4924`, spawned `:5694`) → `_bt_wait_advance` (`:4876`) → `_speaker_back`
(`:4860`) → `_bt_blip_resume` (`:4683`) respawns on reconnect, no tap. This
chain is NOT owned or armed by `_boot_resume`, so removing boot auto-play
does not touch it.

Self-consistency (no special-casing needed): at boot the box is paused, so
`_bt_transport_lost` sees `_mpv_alive()` False and `spotify_playing()` False
and returns WITHOUT arming `lost` (`daemon.py:4757-4773`). A reconnect before
the first tap therefore does nothing — the kid must tap first. Only after a
tap starts playback does a later drop arm auto-resume. Exactly the intent.

The paused-and-shown state ALREADY EXISTS — B is a deletion, not new state:
- ghost-session card in `status()`: spotify `daemon.py:2717-2736`, mpv
  `2737-2776` — presents a bookmarked target as paused-at-position,
  `out["playing"]=False` when nothing spawned. (2026-08-10 field fix.)
- `ui.py:_boot_landing()` `3778-3819` — lands on now-playing for a live
  session OR a bookmarked-paused ghost; expired → carousel. Gated on
  `status.title` + `session != "expired"`, both satisfied without playback.

## Changes (Option D — two deletions)

### 1. `pi/daemon.py` — `_boot_resume` (~5269-5357)
Remove the PLAY TAIL only: the grace loop, the `_kick_bt_connect` at
`:5318`, the spotify/wifi waits, the blip-claim, and the final
`ORCH.play(target, reverse=..., resume=True, boot=True)` (`~5356-5357`).
Roughly lines `5301-5357`.
KEEP: the function itself and its thread (pinned by
`tests/session_window.py`), and all early guards
(`is_sonos` early-return, `resume_window_h==0`, the `LAST_FILE` read, the
`was_playing` one-shot consume + rewrite at `:5287`, the `session_verdict`
expired check). The function should return after consuming `was_playing`
and judging the verdict — it just no longer spawns audio.

### 2. `pi/daemon.py` — sonos reconcile `else` branch (~4188-4216)
Delete `4201-4216`: the `if load_settings().get("resume_window_h") != 0 …
ORCH.play(ORCH.target, resume=True, boot=True)` block that moves a prior
Sonos session onto the box on reboot.
KEEP `4193-4200` — these MUST still run every boot:
`_renderer.write("box")`, `content.PREFER_REMOTE = False`,
`_library._EXPAND_CACHE.clear()`, and the `ORCH.source` reset off "sonos".

### Do NOT touch
- `_kick_bt_connect` inside `play()` (`daemon.py:831`) — real taps still
  wake the BT speaker.
- `_bt_transport_lost` and the whole `lost`/`_bt_wait_watcher`/
  `_bt_blip_resume` chain — that IS the owner's rule 2.
- The adopt branch (`4114-4186`, live Sonos session at boot re-attaches) —
  not the complaint, and it starts no box audio.

## Tests

Break — rewrite from "boot plays" to "boot does NOT play, does NOT kick":
- `tests/boot_resume_guard.py` tests 5-9 (assert `PLAYED == [...]` and
  `KICKED`). Tests 1-4 (the `play(boot=True)` race guard) are UNAFFECTED —
  `play()` is untouched. This file's second half is the bulk of the churn.
- `tests/session_stamp.py` point 6 (~101-109) — regex-pins
  `ORCH.play(…resume=True, boot=True)` in `_boot_resume`. Update/remove.
- `tests/resume_overlap.py` point 7 (~107-110) —
  `DSRC.index("ORCH.play(target, reverse=bool(last.get(\"reverse\")),")`
  throws ValueError once the call is gone. Update/remove.

Unaffected (verified) — leave alone, they guard the invariants we keep:
- `tests/bt_lost_pause_recover.py` — the mid-session drop→pause→auto-resume
  contract (rule 2). Does NOT reference `_boot_resume`. This is the
  regression guard proving rule 2 still works after boot lands paused.
- `tests/session_window.py` (only needs the thread to exist — kept),
  `tests/ui_session_landing.py` (already tests the paused-ghost landing —
  validates this change), `tests/sonos_renderer.py`,
  `tests/sonos_contract.py`, `tests/spotify_resume.py`,
  `tests/episode_resume.py`, `tests/output_switch_resume.py`.

Add one new pin (recommended): boot with `was_playing=True` leaves
`ORCH` NOT playing and lands on the paused now-playing target — the positive
assertion that boot is silent but remembered.

## Verify

`python3 tests/run_all.py` green (148 files today). In field:
1. Play something on the box speaker, power off, power on → lands on
   now-playing PAUSED, one tap continues from the right second.
2. Same after a Sonos session → box does NOT start the built-in speaker
   (the specific complaint); lands paused on the box.
3. Play on the box speaker over BT, pull the speaker's power briefly so BT
   drops, restore it → audio auto-continues on reconnect, no tap (rule 2
   intact).

## Rejected
- A (`resume_window_h = 0`): also kills the paused-at-position landing —
  wakes in the carousel, the "box forgot" regression. Rule 2 would still
  work (independent path) but rule 1's quality is lost.
- C (leave as-is): fails rule 1.
