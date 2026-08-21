# PLAN: boot lands PAUSED; BT-reconnect mid-story still auto-resumes

> **IMPLEMENTED 2026-08-20** — and further than planned: instead of the
> two surgical deletions, `_boot_resume` was deleted WHOLE (function +
> thread), owner-approved. Post-plan findings that justified it: the
> landing survives because `ORCH.target` is restored from LAST_FILE in
> `__init__`, not by play(); the verdict has its own boot thread; change
> #2 removed the last reader of `was_playing`, making the flag
> write-only (still written — honest shutdown record, TERM-race contract
> pinned by spot_boot_flag.py); and btwatchd's own BOOT state pages the
> speaker at boot regardless, so dropping `_kick_bt_connect` costs
> nothing (see the section added below). play()'s `boot=True` guard is
> KEPT with no caller, as the documented contract for any future boot
> starter. Tests rewritten: boot_resume_guard (second half),
> session_stamp #6, resume_overlap #7, session_window #7. Suite green,
> 148 files. AWAITS the field test in Verify below. Part 2 (the Sonos
> hiccup) is NOT built. Part 3 resolved as not-a-bug.

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

### Why dropping `_kick_bt_connect` at :5318 costs nothing (verified 2026-08-20)

btwatchd already pages the speaker at boot on its own: `enter_boot()` /
`_boot_tick()` (`btwatchd.py:357-370`) run a BOOT state with
`BOOT_WINDOW_S = 120` and up to `BOOT_FAIL_LIMIT = 4` attempts, holding
while wifi is still associating (`_radio_yield`). That path is entirely
independent of `_boot_resume`, so a box with bt output still connects at
boot after this change.

What `_kick_bt_connect` adds on the boot path (`daemon.py:4967-4984`) is
(a) the bt_waiting POPUP via `_BT_WAIT["since"]`, and (b) a KICK_FILE
write that bypasses btwatchd's blind-retry backoff. Both are right to
lose here: the popup asks "connect, or play on the box speaker?" which is
meaningless when nothing is trying to play, and the backoff bypass is
already issued at the moment sound is actually wanted — `play()` calls
`_kick_bt_connect()` at `daemon.py:831` on the first tap.

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

---

# PART 2: carousel play on the already-playing tile hiccups on Sonos

> **IMPLEMENTED 2026-08-20.** The guard lives in play(), before the
> is_sonos early return — NOT in sonos_start_target, which lacks `fresh`
> in its signature (the condition the plan below forgot; both local
> shortcuts gate on it). All three traps below are covered and pinned by
> tests/sonos_same_tile.py: playing -> no-op, paused -> one /resume with
> the optimistic flip, untrusted map / broken queue mapping / press in
> flight (8s pending+opt_tr settle window) / stale snap / fresh /
> explicit episode all fall through to the full transfer. Suite green,
> 149 files. AWAITS the field verify below.

Owner (2026-08-18): pressing play/A in the carousel on the tile that is
ALREADY playing over Sonos gives a small audible hiccup. Same root class as
Part 1 (playback-start), same "do it with a clear head" caution — Sonos code
is field-hardened.

## Root cause (confirmed in code)

`ORCH.play()` routes Sonos through an UNCONDITIONAL early return
(`daemon.py:797`):
```
if _renderer.is_sonos() and not boot:
    _radio.touch_busy()
    return self.sonos_start_target(target, episode=episode)
```
`sonos_start_target` (`daemon.py:1395`) always re-expands the queue, re-reads
the bookmark, and re-pushes the episode to the speaker. For Storytel that
re-mints a signed URL and re-pushes the DIDL — the hiccup.

The LOCAL paths already avoid this: `play()` has an "already loaded → unpause,
don't restart" shortcut for mpv (`~832`) and Spotify (`~849`). The Sonos path
has NO such guard. And `ui.py:handle_carousel`'s own comment states the
intent — "A never restarts anything" — so the Sonos path is simply missing a
guard that was always meant to be there. This is a real defect, not
unavoidable behaviour.

## Fix shape (NOT yet built)

Add a same-target-already-playing guard for Sonos, mirroring the local
shortcut. Before the `is_sonos()` early return (or at the top of
`sonos_start_target`): if `target == self.target`, `self.source == "sonos"`,
`episode is None` (an explicit episode pick must still seek), and the speaker
is playing OUR live session of it — from `self.sonos_snap`: `ours` is True,
`transport == "PLAYING"`, and the snap is fresh — then it's a no-op: return
without touching the speaker (the UI just opens now-playing).

State to read: `self.sonos_snap` (`{"ours","transport","stale_s",...}`, set
in `_sonos_play_entry` ~1208 and refreshed by the poller). Freshness: the
poller already treats `transport == "PLAYING"` with `age < 60` as live
(`daemon.py:1039`); reuse that notion, don't invent a new one.

## The trap — why this needs a clear head, not now

- **Map drift / heal.** When `sonos_map_trusted` is False, a press is meant
  to RE-SYNC (re-transfer). A naive "already playing → no-op" must NOT
  swallow that heal. Only skip when the session is genuinely ours-and-live
  AND the map is trusted (spotify) / the url session is intact.
- **Paused vs playing.** If the same tile is PAUSED on the speaker, A should
  resume it (like the local unpause), not restart and not no-op. Decide both
  branches explicitly.
- **Optimistic holds.** `sonos_pending` / `sonos_opt_tr` mean a jump was JUST
  issued; a guard reading a stale snap mid-jump could wrongly skip a real
  press. Check these too, or scope the guard to "no press in flight".

## Tests

- Add: play on Sonos, then A on the SAME target while it's playing → no
  second `_renderer.post("/queue_play"...)` / no `_sonos_body` re-mint, and
  no DIDL re-push. Assert the speaker is not re-commanded.
- Add: same tile PAUSED on the speaker → A resumes (one command), does not
  restart from the bookmark.
- Add: `sonos_map_trusted=False` → A still re-transfers (the heal path is
  not swallowed).
- Check `tests/sonos_renderer.py` / `sonos_contract.py` for existing
  press-idempotence pins before writing new ones.

## Verify in field
Play a Storytel book on Sonos, browse to carousel, press play on the same
tile → no hiccup, lands on now-playing. Then pause on the speaker, press play
on the tile → resumes cleanly. Then a real target-switch still starts the new
book (guard didn't over-match).

---

# PART 3: a long tile name never scrolls all the way across

Owner (2026-08-20): "in the carousel at least — if the tile name is too
long, the whole name doesn't scroll across the screen." Note the hedge:
they suspect it isn't only the carousel.

## STATUS: root cause NOT established — do not fix yet

Architect + QA ran on 2026-08-20 and DISAGREED. QA refuted the leading
hypothesis with arithmetic. The architect's plan was built on that
refuted premise and is discarded. What follows is what survived.

### Refuted: "the 20-char window is a character budget on a pixel screen"

The theory was that `_cover_tile` (`ui.py:3588`) calls `marquee(name, 20)`
with no pixel measurement — unlike `render_now`, which gates on
`d.textlength(title, font=F_MED) > W - 44` (`ui.py:3194`) — so a wide
20-char name is drawn centered (`anchor="ma"`) and clipped at both screen
edges, hiding head and tail. Two independent checks killed it:

- The slide DOES reach both ends. `span = len+n-maxlen`,
  `period = span+8`, `off = max(0, min(span, step-4))` — at `off=span` the
  window is `text[-maxlen:]`. Ran it for a 31-char name: both `text[:20]`
  and `text[-20:]` appear. `tests/ui_marquee.py:43` already pins this.
- 20 chars of DejaVuSans at `font(17)` is ~178px (mixed case) to ~232px
  (all caps) on a 240px panel. It does not clip. UNVERIFIED — no DejaVu
  in the dev container; measured from published metrics.

The real mismatch is the INVERSE and is cosmetic, not the reported bug:
the full-width tile gets `maxlen=20` while the NARROWER list rows get 24
(`draw_list`, `ui.py:1276`; `17 if art else 24` at `2988/2995/3004`). The
tile could carry ~25 mixed-case chars. It slides names that would have fit.

### Confirmed defect (real, but destroys the START, not the end)

The emoji surcharge `n` inflates `span` but is NOT applied to the raw
slice indices `text[off:off+maxlen]` (`ui.py:1265-1273`). When
`len(text) <= maxlen < len(text)+n`, `off=0` already holds the WHOLE
name, so the animation eats leading characters and reveals nothing new,
then rests on the mutilated version. Reproduced (18 chars, n=4): steps
0-4 show all 18, step 5 drops the first char, steps 7-9 rest at
`text[2:]`. Fix is small, but it is NOT what the owner reported unless
the failing name has emoji.

### FIELD READING (owner, 2026-08-20) — the candidates below are settled

Failing title: **`Jakten på jungelens dronning`** — 28 chars, no emoji.
Symptom: "stops after scrolling a few letters, then starts over at the
beginning."

Traced through `marquee(name, 20)` exactly: `span = 28-20 = 8`,
`period = 16`, full cycle **5.6s**. Steps 0-4 rest on
`'Jakten på jungelens '`; steps 5-12 slide ONE character each; steps
12-15 rest on `'å jungelens dronning'`; then it wraps. The label travels
**8 characters in 2.8s** and snaps back. That IS the reported symptom —
designed behaviour, not a fault.

This settles the candidate list:
- **Candidate 1 (`NOW_RETURN_S = 10`) — DEAD for this title.** The full
  cycle is 5.6s, well inside 10s, and the owner SEES the wrap-back,
  which is impossible if the view were yanked away.
- **Candidate 3 (`screen_timeout_s`) — DEAD.** Same reasoning.
- **The emoji surcharge defect — not this bug.** No emoji in the title.
  Still a real defect (see above); fix it separately or not at all.
- **The 20-char budget — THIS IS IT.** `'Jakten på jungelens '` is 20
  mostly-lowercase chars ≈ 180px on a 240px panel. The tile is
  throwing away ~60px of screen, which is what forces a 28-char title
  to scroll at all, and forces the crop to land mid-word
  (`'akten på jungelens d'`). A full-width tile has the SMALLEST budget
  in the UI — the narrower list rows get 24 (`ui.py:1276`).

### RESOLVED 2026-08-20: not a UI bug — the library entry name is 28 chars

Final field reading: the label jumps back to the start right after
`Jakten på`, with the screen still LIT and nothing playing. That is
decisive, because of one property of `marquee`: `off` maxes at
`span = len+n-maxlen`, so the resting window is `text[len-maxlen:]` —
**the last `maxlen` characters**. The final frame before a wrap therefore
always ends on the string's last character. Ending on `Jakten på` means
the string ENDS there.

The book is `Detektivbyrå nr.2: Jakten på jungelens dronning` (47 chars),
but the library entry is named `Detektivbyrå nr.2: Jakten på` — **28
chars**. `span = 28-20 = 8`, so it slides exactly 8 characters and wraps:
precisely "scrolls a few letters, then starts over", the owner's words
from the first report.

Nothing truncates it — it is a SERIES tile, and that IS the series name
on Storytel's side. Confirmed by the owner:
`storytel.com/no/series/detektivbyrå-nr-2-jakten-på-139545`. The tile
takes `si.get("name") or model.get("title")` (`storytel.py:469`), which
correctly prefers the series name for a series group; the individual book
title (`Jakten på jungelens dronning`) lives on the book inside it.

So the code is right at every layer: the PWA's entry-name input has no
`maxlength` (`pi/web/index.html:91` — the `maxlength="32"` at `:306` is
the wifi SSID field), `library.py:73` stores `ename` raw, and the shelf
endpoint (`daemon.py:3510`) is read-only, so renaming the entry in the
PWA sticks across syncs. **NOTHING TO FIX. Rename the entry if the
series name reads badly on a tile.**

Everything else in this section was chasing a bug that wasn't there.
Three hypotheses were raised and all three are dead:
- pixel-vs-character clipping — refuted by arithmetic and by the fact
  that the head of the name IS visible
- `NOW_RETURN_S` snap-back — requires `status.playing`; nothing was
  playing
- repaint starvation / screen sleep — the screen stayed lit

### STILL LATENT (worth fixing on its own merits)

With the FULL 47-char name the cycle would be `(27+8)*0.35 = 12.25s` and
the tail would first appear at `(27+4)*0.35 = 10.85s` — while
`NOW_RETURN_S = 10` (`ui.py:607`) snaps the view to now-playing at 10.15s
whenever something IS playing (`ui.py:3969-3974`, list includes
`"carousel"`). So the moment this entry is renamed to its real title, a
genuine bug appears: the end of the name becomes unreachable during
playback, by 0.7s.

Cheapest correct fix: stand the `NOW_RETURN_S` check down while
`marquee_active` is true — the box already tracks that flag
(`ui.py:3050`) and uses it to drive the repaint gate. Don't yank the
screen away mid-name. Raising `NOW_RETURN_S` only moves the threshold to
longer titles; a wider label budget alone gives 9.1s vs 10s, too close.

Also still open, independent of all the above: the emoji-surcharge
off-by-N in `marquee` (`ui.py:1265-1273`), which eats leading characters
and reveals nothing for names that overflow only because of the charge.

### SUPERSEDED — the 47-char reasoning, kept for the record

The first reading gave a partial name. The full tile name is
**`Detektivbyrå nr.2: Jakten på jungelens dronning`** — **47 characters**.
Everything below that reasons from 28 chars is wrong, including the
"candidate 1 is DEAD" call. Recomputed:

- `span = 47-20 = 27`, `period = 35`, full cycle **12.25s**
- the tail (`off=27`, `'å jungelens dronning'`) first appears at
  `(27+4) * 0.35 = ` **10.85s**
- `NOW_RETURN_S = 10` (`ui.py:607`) fires at **10.15s**, snapping the
  view to now-playing (`ui.py:3969-3974`, list includes `"carousel"`)

**The tail is unreachable by 0.7s whenever something is playing.** The
owner's "Jakten på is the last thing I see" matches: at 8.05s the window
is `'Jakten på jungelens '`, and the last ~2s before the yank slide it
off to the left. QA called this candidate at a ~45-char threshold; it was
wrongly dismissed on the truncated 28-char title.

Confirm by repeating with NOTHING playing: the snap-back is gated on
`self.status.get("playing")`, so the cycle should complete in 12.25s and
the tail should appear for 1.4s. If it does, this is settled and the
pixel/clipping theory is dead for good.

Fix candidates (decide after the confirmation):
- suppress the `NOW_RETURN_S` snap-back while `marquee_active` — the
  view must not be yanked mid-name. Cheapest and most targeted.
- raise `NOW_RETURN_S`, or make it "10s AND no marquee in flight".
- shorten the cycle so it fits inside 10s regardless: a wider label
  budget cuts `span` (a 47-char title at a pixel-correct ~25-char window
  needs `(22+4)*0.35 = 9.1s` — still uncomfortably close).
Note the first two are the real fix; the budget change alone only moves
the threshold to longer titles.

### SUPERSEDED — reasoning from the truncated 28-char title

Asked the owner whether `dronning` becomes readable at the end of the
slide. Answer: **no, the end never appears.**

That contradicts the arithmetic, which is not in doubt: the window rests
on `'å jungelens dronning'` for 4 steps (1.4s) before wrapping. If that
window is drawn and the word still isn't readable, the window is being
CLIPPED BY THE PANEL — i.e. the "refuted" pixel hypothesis is alive after
all, and the char-vs-pixel budget is a genuine defect, not cosmetics.

Ruled out as explanations of the contradiction:
- repaint starving the last steps — the gate is
  `marquee_active and now - last_render >= MARQUEE_STEP_S`
  (`ui.py:3991-3994`), ~0.35s, and ~3 renders land on the final rest.
- a `RichDraw` centering bug — a title with no emoji delegates straight
  to Pillow's own `anchor="ma"` (`ui.py:419-421`); the custom centering
  math at `ui.py:430-433` is never reached.
- `NOW_RETURN_S` / screen sleep — both far longer than the 5.6s cycle.

THE DECISIVE MEASUREMENT (run ON THE BOX, where DejaVu actually exists;
it touches nothing and does not disturb the running UI):

```
python3 -c "
from PIL import ImageFont
f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 17)
for s in ['Jakten på jungelens ', 'å jungelens dronning',
          'Jakten på jungelens dronning']:
    print(round(f.getlength(s)), 'px  |', s)
"
```

Panel is 240px, label centered at `x=W//2`. If `'å jungelens dronning'`
measures **> 240** the tail is clipped: the pixel hypothesis is CONFIRMED
and the fix is a pixel-measured budget. If it measures **< 240** the word
IS on screen and the report is about legibility/pace, not clipping — then
mock up the font-shrink or two-line options below instead.

Note `font()` (`ui.py:623-631`) falls back to `load_default(size)` when
DejaVu is missing. Confirm the truetype load actually succeeds on the box
— a fallback bitmap font would change every width in this analysis.

### Fix direction (pending the answer above)

The title is only ~10-15px too wide for the panel (UNVERIFIED — no
DejaVu here). So widening the budget to a pixel-correct value does NOT
remove the scroll, it shrinks `span` to ~3 and makes the nudge look
MORE broken, not less. The window-slide model reads as a fault for
titles that barely overflow. Options, cheapest first — MOCKUP BEFORE
CODING (render with ui.py's own constants):
- **Shrink the label font for overflowing tile names** so the whole name
  fits and nothing animates. ~6% off F_MED(17) buys ~15px.
- **Wrap the tile label to two lines** like `render_now` does via
  `wrap_two`. Check the space between the label at y=206 and the
  playing-underline at y=228 — it may not be there.
- **Widen to a pixel-correct budget** and accept a small nudge.
- **A true ticker** (sweep the whole title past, off one edge and back)
  — biggest change, and it fights the "rest at the start" anchor added
  2026-08-12.

### Superseded candidates (kept for the record)

1. **`NOW_RETURN_S = 10` yanks the view away** (`ui.py:607`, applied at
   `ui.py:3969-3974` to `"home"/"entries"/"episodes"/"carousel"/"cats"`
   whenever `status.playing`). The tail first appears at
   `(span+4)*0.35`s, which passes 10s at ~45 characters. Browse with
   music playing, land on a long tile, and now-playing snaps in before
   the name finishes. Carousel-specific in effect — matches the hedge.
2. **The pace is slower than a kid.** 4 resting steps = 1.4s before the
   name moves at all, then 0.35s/char. A 40-char tile needs ~8.4s to
   show its tail, and every B/Y flip re-anchors `_marquee_t0("tile",
   name)` (`ui.py:3589`) back to step 0. No defect; fully reproduces the
   words.
3. **`screen_timeout_s = 15`** (user-settable, `ui.py:2728`; default 30
   at `ui.py:1367`) blanks the panel at ~46 chars. Only if the box is
   set to 15.
4. **`render_now`'s own gate/threshold mismatch** (`ui.py:3194`, `3209`):
   gates on pixels, then delegates to a 20-CHAR threshold, so an 18-wide-
   char title passes the gate and returns `scrolling=False`, drawn wider
   than the `W-44` the layout budgeted, under the side markers. Real, but
   not the carousel.

Falsified and not worth re-testing: the marquee not animating at all
(`rolls` → `marquee_active` `ui.py:3050` → repaint gate `ui.py:3991`
fires every ~0.4s; ~1 step in 8 is skipped, cosmetic only);
`_mq_key`/`_mq_t0` thrash (all 12 `_marquee_t0` sites are in mutually
exclusive `if/elif` branches, one key per frame); `_slide`'s
`marquee(label, 20, t0=time.monotonic())` (`ui.py:3615`) poisoning the
phase (it bypasses the anchor; the landing render re-anchors).

## The discriminating field test — do this BEFORE writing code

Land on the offending tile and DO NOT TOUCH THE BOX for 30 seconds.
Record which happens, plus the exact name, its character count, and
whether it contains emoji:

1. screen switches to the now-playing layout → candidate 1 confirmed
2. screen goes black → candidate 3 (`screen_timeout_s` is 15)
3. tile stays and the name completes its slide → no timing defect;
   it is the pace (candidate 2) plus the 20-char budget
4. name is ≤20 chars, has emoji, and SHRINKS FROM THE FRONT → the
   confirmed surcharge defect above

Repeat with nothing playing to separate 1 from 3 definitively.

One 30-second hands-off trial separates every remaining candidate. Until
that reading exists there is nothing to implement here.

---

# PART 4: Sonos group-awareness

Owner (2026-08-21): the picker should show and select speaker GROUPS,
refresh the list when the output picker opens, and the box should not
have to manage grouping — the Sonos app already does that well.

## Stage A — IMPLEMENTED 2026-08-21 (display + selection + fresh list)

The cheap primitive, already named by the RF audit at `sonosd.py`
(2026-08-10 #2): **GetZoneGroupState against any one cached ip returns
the whole household in one ~200ms call** — every zone (uid, name, ip),
every group, each group's coordinator. SSDP (3s+ multicast) degrades to
cold-start fallback.

- `sonosd.refresh_topology()`: merges zones (heals DHCP moves and
  renames for free), REPLACES the group map wholesale (a snapshot),
  filters bonded invisibles (stereo pairs / subs are not rooms),
  coordinator first in each member list. Raises when nobody answers;
  `/players?fresh=1` then serves the cache with `stale: true` — the
  cabin case shows the truth, not home's ghosts.
- daemon `/sonos?fresh=1` passes it through (timeout 6).
- ui: hold-X still gates the Sonos row on the CACHE (instant, per owner
  2026-08-09) and kicks the fresh fetch in the background, so the
  speaker submenu is current by the time a finger gets there. The
  submenu's old background SSDP became topology-first, SSDP only on
  the stale marker. `_sonos_choices()` is the ONE source for both
  display and selection: a multi-member group is one row
  ("Kjøkken + Stua", coordinator first), selecting it stores the
  COORDINATOR's uid — transport verbs on a coordinator drive the whole
  group, so everything downstream is unchanged. Absorbed members do
  not repeat; a group naming an unknown uid is skipped whole.
- Pinned by tests/sonos_groups.py.

Answers to the owner's two questions, as built: (1) yes — the list
refreshes when the output picker opens, but async behind the instant
cache read, so hold-X never waits on the network; (2) the Sonos row
does NOT disappear off-LAN, before or after the probe, ON PURPOSE:
hiding it up front would need a blocking probe in hold-X, and removing
a row from an OPEN menu moves the remaining rows under the finger that
was about to press one — the classic mid-menu trap. The cache's merge
semantics also never delete a speaker (a scan that misses one over
multicast drops must not delete the row the kid was aiming at). What
the stale marker does instead: the speaker submenu's hint line flips
from "A: play here" to "No speakers answered here" (owner follow-up
2026-08-21), and a press on a ghost fails cleanly as before. The row
heals itself the moment the box is back on a network where someone
answers.

## Stage B — NOT BUILT: follow the coordinator mid-session, group volume

What stage A deliberately leaves: if someone REGROUPS in the Sonos app
while our session plays, our chosen uid can become a group MEMBER. The
sidecar already detects it (`grouped_away` + `coordinator` in the aux,
`sonosd.py` ~590; daemon surfaces `renderer_state: "grouped-away"`) but
nothing acts on it.

- **Follow, don't fight:** when grouped_away, the sidecar should target
  the COORDINATOR for transport verbs, /play pushes and the poll's
  track/position reads — one seam (an "effective speaker" resolve).
  Trap: a member's AVTransport reports `x-rincon:<coordinator>` as its
  uri, not the track — the `ours` check must read the coordinator or it
  logs not-ours forever. Trap: pushing DIDL to a member RIPS it out of
  the group. This is field-hardened poller/verb code — architect + QA
  round before building, same rule as Part 2.
- **Group volume:** when grouped, the volume card should drive
  GroupRenderingControl (SetGroupVolume) on the coordinator, or the kid
  turns one room only.
- Group create/dissolve stays OUT (owner): the two verbs are trivial
  (join = SetAVTransportURI x-rincon:<coord>; leave =
  BecomeCoordinatorOfStandaloneGroup) and belong in the PWA settings
  page if field use ever asks for them — never in the kid-mode button
  language.
