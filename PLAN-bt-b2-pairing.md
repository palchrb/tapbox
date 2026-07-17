# Plan: B2 (pair over D-Bus) + incoming pairing mode ("pair from car")

Status: **implemented 2026-07-17, rig gates pending** — tasks 1–7 of §5
landed (btbus agent + `_dbus_pair` + `pairing_window`, fake_bluezd
growth, tests/bt_pair_dbus.py + tests/bt_visible.py, `bt.py visible`,
`POST /bt/visible`, PWA "Pair from car", docs). The dbus-gated test
sections SKIP on dev machines and must be run ON THE RIG; task 8 (flip
`TAPBOX_BT_PAIR` default) stays gated on §6. Window clamp ended up
10–300s (floor lowered from 30 for testability). Drafted 2026-07-16 by
three parallel review passes (architect / implementer / QA), then
reconciled. Parent plan: [PLAN-bt-dbus.md](./PLAN-bt-dbus.md) (phases
A0/A1/B1/C done; this document is the B2 cut, extended with the
incoming-pairing feature the car scenario exposed). Guiding values:
**robustness and simplicity** — the boring option wins ties.

## 1. Goal & scope

Two features on one shared foundation (our own `org.bluez.Agent1`):

1. **B2 outgoing**: replace `btbus.pair()`'s bluetoothctl fork + regex
   classification with `Device1.Pair()` + a NoInputNoOutput agent. The
   cli path stays as the permanent fallback AND as the default until
   the rig matrix passes (kill switch, same philosophy as A1/B1).
2. **Incoming pairing mode** (new): today the box can only pair
   *outward* (box scans, box pairs). A car head unit that says "put
   your device in pairing mode" cannot work: nothing ever sets
   `Discoverable=true`, and no agent exists to answer an incoming SSP
   dance. New verb `bt.py visible [secs]` + `POST /bt/visible` + PWA
   button "Pair from car": a ~2-minute discoverable window with a
   default agent that auto-accepts, trusts the new bond, and reports it.

Out of scope (explicit): Class of Device / main.conf change (deferred
rig experiment — some head units filter the scan list to phones/audio;
the box advertises "computer"; needs the real car to pick a value);
auto-adopting the new device as output (see D6); btwatchd changes of
any kind; recover()/_hci_crashed()/rfkill (stay subprocess/sysfs, run
before any bus call); removing the cli pair path; changing the
`Pairable` always-on posture (bonding gotcha, bt.py docstring);
multiple pairings per window; AVRCP/car-as-controller.

## 2. Decisions (reconciled)

**D1 — Agent code lives in btbus.py's dbus section** (new subsection
`# --- dbus backend: pairing agent (B2) ---`), not a new module. The
agent is transport machinery, `pair()` and its fallback already live
here, and one file keeps one seam for the tests. btbus's "no mainloop"
invariant survives in the form that matters: the GLib loop is per-call,
runs only on a **dedicated private bus connection**, and is fully torn
down in `finally` — the process's shared connections and blocking-call
model are untouched. (Architect proposed a separate `btagent.py`;
rejected for module-count simplicity — revisit only if the subsection
outgrows ~250 lines.)

**D2 — Agent lifecycle per process:**

| Process | Agent? | Lifetime |
|---|---|---|
| bt.py CLI `use`/`connect`/`ensure` | yes, non-default | inside `btbus.pair()` only: register → async Pair → loop → unregister |
| bt.py CLI `visible` | yes, **default** (`RequestDefaultAgent`) | the window; auto-unregistered by bluez if the process dies (registration dies with the bus connection) |
| btwatchd | **never** | amendment to parent plan §5, see §9 |
| tapboxd | never | forks bt.py for radio work, as always |

**D3 — Kill switch: `TAPBOX_BT_PAIR=cli|dbus`, default `cli`,** read
per-call inside `pair()` (not cached — testable, and a `systemctl edit`
needs only a restart). dbus pair runs only when `backend()=="dbus"`
AND `TAPBOX_BT_PAIR=dbus`; any infrastructure exception (bus gone, gi
missing, registration failed) degrades per-call to the cli fork with
the standard one-line log — identical shape to `connect_device()`.
Separate from `TAPBOX_BT_BACKEND` because "reads/actions dbus, pair
cli" must stay expressible. Flipping the shipped default is a one-line
change gated on the rig matrix (§8); the env var remains the permanent
per-box escape hatch. `visible` is NOT gated by this switch: it has no
cli equivalent to preserve (bluetoothctl's interactive agent is the
wrong tool unattended) — without dbus/gi it exits 2 with a clear
message, and the box simply behaves as today (feature is additive).

**D4 — One concurrency pattern, no threads:** dedicated private bus
connection (`mainloop=DBusGMainLoop()` kwarg at creation — the shared
`SystemBus()` singleton was created loop-less and can never export an
object) + async `Pair(reply_handler, error_handler, timeout=60)` +
`GLib.MainLoop().run()`. Agent callbacks are incoming calls on our
connection; only a running loop dispatches them — a blocking `Pair()`
deadlocks on every legacy-PIN device (parent plan §9.1). A worker
thread would still need a loop somewhere; it buys nothing. A
`GLib.timeout_add_seconds(75)` guard bounds the pathological
never-answered case. Closing the private connection in `finally` IS
the cleanup: bluez auto-unregisters agents whose connection dies.

**D5 — Window robustness core: `DiscoverableTimeout=secs` is set
BEFORE `Discoverable=true`.** bluez itself then clears Discoverable
after the timeout — SIGKILL, bus drop, or a Python crash mid-window
can never leave the box permanently visible. Our explicit
`Discoverable=false` on exit is best-effort tidiness on top. flock is
held for the whole window (verb joins `_RADIO_CMDS`), so btwatchd's
LOCK_NB defers — critical because the car scenario *guarantees* the
configured speaker is absent, i.e. btwatchd is actively paging at that
exact moment (the documented firmware crasher). The daemon quiesces
playback around the window like `/bt/pair` does, same reasoning.

**D6 — Adoption policy: trust + report, never auto-adopt as output.**
Auto-adoption would let whatever paired during the window seize the
kid's audio (neighbor risk), and would fire `_disconnect_others` +
ALSA rewrite + go-librespot restart from a background flow — the
side-effect class the output-policy work spent weeks debouncing. The
window trusts the new bond (required: the car's A2DP service
authorization arrives after our agent is gone; `Trusted` bypasses
`AuthorizeService`) and reports it; the PWA then offers "Use as
speaker" → the existing `POST /bt/connect` — the full battle-tested
adopt path, one tap away. `bt.py visible <secs> adopt` keeps the
auto-variant available as a CLI arg for later product choice; the
daemon does not pass it.

**D7 — Endpoint stays synchronous through `bt_action`** (timeout
`secs+150` covers window + worst-case `bt_up`/recover). No state file,
no background runner, no cancel machinery: the window is ≤300s and
self-clearing (D5), `bt_status().pairing` (= `BT_LOCK.locked()`) is
already the PWA's busy signal, and the window quits ~3s after the
first successful pairing so the happy path returns fast. (Architect
proposed a Popen + JSON state file + cancel; rejected as machinery for
a rare, bounded wait. Revisit if the PWA needs a countdown/cancel.)

**D8 — Security posture:** Discoverable is never true outside a window
(verified: nothing in the codebase touches it today). The default
agent exists only inside the window process and dies with its
connection; outside a window an unsolicited pairing finds no agent and
cannot complete authorization. One pairing per window (Discoverable
off + linger + quit on first success) blocks drive-bys. Residual,
accepted, documented: `Pairable` stays always-on (bonding gotcha) — a
device that already knows our MAC could attempt Just-Works pairing but
gets no service authorization without an agent.

## 3. File-by-file changes

### pi/tapbox/btbus.py (the bulk)
- Constants `_AGENT_PATH = "/org/tapbox/agent"`, capability
  `"NoInputNoOutput"`.
- `_map_pair_error(name, msg)` — pure classifier next to
  `_map_connect_error` (unit-testable, no bus):
  `.AlreadyExists`→PAIR_ALREADY; `.AuthenticationFailed/Canceled/
  Rejected/Timeout`→PAIR_AUTH_FAILED; `.ConnectionAttemptFailed` +
  `UnknownObject`/`UnknownMethod`→PAIR_NOT_AVAILABLE; `.InProgress`/
  `.Failed`/`NoReply`/other→PAIR_ERROR (keep `f"{name}: {msg}"` for
  the log).
- `_agent_bus()` — private connection with per-connection mainloop;
  honors `TAPBOX_DBUS_ADDRESS`/`DBUS_SYSTEM_BUS_ADDRESS` like `_bus()`.
- `_make_agent(bus, on_event=None)` — factory (class defined inside to
  keep dbus.service import lazy). Full Agent1 repertoire: `Release`/
  `Cancel` no-op, `RequestPinCode`→"0000", `RequestPasskey`→UInt32(0),
  `RequestConfirmation`/`RequestAuthorization`/`AuthorizeService`→
  allow, `DisplayPasskey`/`DisplayPinCode` no-op. Exact signatures
  matter (`o`,`ou`,`os`,`ouq`; out `s`/`u`) — a mismatch looks like an
  absent agent and misdiagnoses as an SSP problem.
- `_register_agent(bus, default=False)` / `_unregister_agent(bus)` —
  AgentManager1 at `/org/bluez`; `.AlreadyExists` tolerated;
  `RequestDefaultAgent` only when `default=True`; unregister
  best-effort in `finally` (connection close is the real cleanup).
- `_dbus_pair(mac)` — async pattern per D4; `introspect=False` on the
  device proxy (skip the blocking round-trip); classify via
  `_map_pair_error`; returns `(PAIR_*, detail)`.
- `pair(mac)` — kill-switch gate per D3; `_cli_pair(mac)` = today's
  body, renamed verbatim.
- `pairing_window(secs)` — per D5: `Pairable=True` (explicit),
  `DiscoverableTimeout=UInt32(secs)`, `Discoverable=True`, register
  default agent, subscribe `PropertiesChanged` (+ `InterfacesAdded`)
  for `Device1 Paired: true`; on first pair: `_dbus_trust(mac)` INSIDE
  the window, record `{mac, name}` (Alias via GetAll, `.get()`
  everything), linger 3s (post-pair service auth), quit. Window-end
  timer `secs`. `finally`: Discoverable off (best-effort), unregister,
  `remove_from_connection()`, `bus.close()`. Raises RuntimeError with
  a human message when dbus/gi are missing. Returns `[{mac, name}]`.
- Test enabler: `populate_cache` seconds in `bt.connect()` become
  `TAPBOX_BT_CACHE_SECS`-overridable (defaults unchanged) — the
  stale-key test otherwise sleeps ~25s.

### pi/tapbox/bt.py
- `visible(secs=120, adopt=False)` flow verb: `bt_up()` → log "Box is
  visible — start pairing from the car now" → `btbus.pairing_window`
  → log each `==> Paired: Name (MAC)` → report-only by default;
  `adopt` = `connect(new_mac)` (full existing adopt path). Returns
  False when nothing paired.
- `main()` dispatch: `visible [secs] [adopt]`, clamp secs 30–300;
  RuntimeError → message + exit 2. Exit codes: 0 paired, 1 nothing
  paired (or adopt-connect failed), 2 dbus unavailable.
- `_RADIO_CMDS` += `"visible"` (flock for the whole window).
- Module docstring CLI list += the verb.

### pi/daemon.py
- Docstring endpoint block += `POST /bt/visible`.
- `do_POST`: `/bt/visible` branch — clamp secs, `_bt_quiesce()`,
  `bt_action(["visible", str(secs)], timeout=secs+150)`,
  `_bt_resume()`, 409 on busy — mirrors `/bt/pair` exactly.

### pi/web/index.html + app.js
- Bluetooth card: button `#btn-visible` "Pair from car" + one help
  sentence. Handler mirrors `#btn-pair`: disable button, toast "Box is
  visible — start pairing from the car…", 5s `loadBt` interval during
  the call (new bond appears live), re-enable after. `btAction` gains
  an optional `busyMs` param (window outlives the 60s default toast).
  `loadBt()` also disables `#btn-visible` off `bt.pairing`.

### pi/install.sh + README.md
- Deps verified: python3-dbus + python3-gi already installed; bt.py/
  daemon/web already restart-tracked. Only docs: kill-switch comment in
  the tapbox-daemon unit heredoc (`Environment=TAPBOX_BT_PAIR=dbus`),
  README paragraph for `TAPBOX_BT_PAIR` + "Pair from car".

## 4. Test plan

### fake_bluezd.py growth (additive only; existing Mock signatures frozen)
- Adapter props become real state (`Powered`/`Pairable`/`Discoverable`/
  `DiscoverableTimeout`, Set stores + emits PropertiesChanged).
  **Deliberately NO countdown timer on DiscoverableTimeout** — a fake
  that auto-clears would let a bt.py that forgot its explicit reset
  pass; the dumb fake forces the code to prove the reset, and the
  SIGKILL test asserts the *timeout value* was set (bluez's timer is
  the real-world backstop, verified on the rig).
- `AgentManager1` at `/org/bluez`: Register/Unregister/
  RequestDefaultAgent with sender tracking; drop registrations on
  NameOwnerChanged (mirrors bluez; makes killed-mid-window observable).
- `Device1.Pair()` async (`async_callbacks`) that CALLS BACK into the
  caller's registered agent per `SetPairFlow(mac, "just-works"|
  "confirm"|"pin")`; fake-side 15s agent-reply timeout →
  `AuthenticationTimeout` (a deadlocked client fails fast, not hangs).
- `SetPairResult(mac, results)` — space-separated queue consumed one
  per Pair call (`"auth-failed ok"` = the stale-key scenario in one
  line): ok/already/auth-failed/auth-timeout/not-available/in-progress/
  failed.
- `SetPairingMode(mac, b)`: a removed device re-appears (with
  InterfacesAdded) on next StartDiscovery — what a real speaker in
  pairing mode does; RemoveDevice counts `removes`.
- `SimulateIncomingPair(mac)` — drives the DEFAULT agent like a car:
  returns "no-default-agent" when none (that return IS the assertion),
  else AuthorizeService (+ RequestConfirmation per flow) → Paired=true
  + signals → "paired"/"rejected".
- Getters: `GetPairCount`/`GetRemoveCount`/`GetDiscoverable`/
  `GetDiscoverableTimeout`/`GetAgentEvents` (one ordered event log:
  Register/RequestDefaultAgent/RequestPinCode:answered:0000/Unregister/
  OwnerGone).
- NOT faked: SSP capability negotiation, PIN crypto, bonding-vs-non-
  bonding (hardware-only → rig), DiscoverableTimeout countdown,
  InProgress concurrency, LE, multiple adapters. Bus-down = kill the
  fake process.

### tests/bt_pair_dbus.py (the B2 gate — house style, SKIP without dbus)
1. `_map_pair_error` unit table (no bus, all rows).
2. cli classification unchanged (PATH-fake bluetoothctl, five output
   shapes) — health check for the escape hatch.
3. dbus pair OK just-works; event log shows Register BEFORE pair,
   explicit Unregister AFTER (not merely OwnerGone); wall time ≪ 60s.
4. Confirm flow → OK, RequestConfirmation answered-accept.
5. **PIN-flow deadlock regression (the most important B2 test)**: fake
   completes Pair only after RequestPinCode is answered; a blocking
   Pair fails in 15s via the fake's timeout.
6. Full SetPairResult matrix over the bus → expected PAIR_* verdicts
   (same expectation dict as scenario 2 — fixtures can't drift apart).
7. AlreadyExists still trusts (via `bt.py use` subprocess: exit 0,
   Trusted set, Connect happened) — guards the §9.8 trust gap.
8. Stale-key clear-and-retry fires EXACTLY once: `"auth-failed ok"` +
   pairing-mode revive → exit 0, removes==1, pairs==2, Trusted.
9. Never-seen device → PAIR_NOT_AVAILABLE, exit 1, guidance printed,
   removes==0 (must not clear bonds for absent devices).
10. Kill switch: unset → PATH-fake bluetoothctl logs a `pair` fork,
    GetPairCount==0; `=dbus` → no fork, GetPairCount==1. The
    expectation flips in the same commit that flips the default.
11. Bus-down degrade: `=dbus` + dead bus address + PATH fake → pair
    succeeds via cli, fallback log line present.
12. Agent not leaked on failure: `failed` verdict → PAIR_ERROR and the
    event log still ends with Unregister (finally-path).

### tests/bt_visible.py
1. (no dbus needed) POST /bt/visible plumbing with `TAPBOX_PLAY` fake
   CLI: 200 + argv `visible <secs>`; 409 while BT_LOCK held; the
   handler quiesces/resumes like /bt/pair.
2. Happy window `visible 3`: exit code, Discoverable true during +
   DiscoverableTimeout in (0, window]; Register then
   RequestDefaultAgent in the log; after exit Discoverable FALSE
   (provable only because the fake has no timer) + Unregister.
3. Timeout, no visitor: exit 1, MAC_FILE untouched.
4. Incoming pair mid-window (`SimulateIncomingPair(CAR)`): "paired",
   Trusted true, **MAC_FILE untouched** (report-only policy), window
   ends shortly after (linger+quit). Confirm-flow variant too.
5. `adopt` variant: MAC_FILE == CAR + routed (the existing connect
   flow ran).
6. Rejection/never-completes: window runs to deadline, no partial
   adoption (no trust of the failed MAC, no MAC_FILE).
7. flock held for the whole window (LOCK_NB from the test fails
   during, succeeds after).
8. Second `visible` while one runs: blocks on flock (or busy-fails) —
   assert no interleaving; two stacked windows and a silent 60s block
   are both bugs.
9. SIGKILL mid-window: flock immediately acquirable; DiscoverableTimeout
   was nonzero (bluez countdown = the only remaining backstop, rig
   verifies the real one); OwnerGone in the event log; next radio op
   in a fresh process works.
10. No-default-agent guard: SimulateIncomingPair with no window →
    "no-default-agent" (documents: not silently pairable outside a
    window).

### Regression protection
Must stay green unchanged: bt_parity, bt_actions, bt_reconnect,
bt_output_policy, bt_play_kick, bt_stall, wifi_probe_hold + the rest of
the suite. The fake extension lands as its own commit with a full green
run BEFORE any product code. New trap rows for the parent plan §8
table: blocking-Pair deadlock (pair_dbus 5); agent leaked after exit
(3+12); Discoverable stuck on (visible 2+9); window-vs-btwatchd
collision (visible 7 + bt_reconnect 5); trust gap resurrected
(pair_dbus 7); retry loops / bond cleared for absent device (8+9);
kill-switch drift (10); no-bus hang (11); partial adoption (visible
4+6).

## 5. Ordered tasks (every intermediate state shippable)

| # | Task | Risk | Verify (no hardware) |
|---|---|---|---|
| 1 | btbus: `_cli_pair` rename, `_map_pair_error`, `_agent_bus`, `_make_agent`, register helpers, kill-switch gate (default cli — zero behavior change) | low | import with no bus; unit table |
| 2 | fake_bluezd growth (own commit) | low | full existing suite green |
| 3 | `_dbus_pair` + tests/bt_pair_dbus.py | **medium** (the async/agent mechanics) | 12/12, SKIPs cleanly |
| 4 | `pairing_window` + bt.py `visible` + tests/bt_visible.py | **medium** (window/signal plumbing) | 10/10 |
| 5 | daemon `/bt/visible` | low | endpoint test (visible 1) |
| 6 | PWA button + busyMs | low | node --check + stub-server manual check |
| 7 | README + unit comment (docs) | nil | proofread |
| 8 | *(post-rig, separate)* flip `TAPBOX_BT_PAIR` default to dbus | gated | rig matrix §6 |

## 6. Rig checklist (manual, hardware)

**Outgoing dbus pair (gate for flipping the default):**
1. Factory-reset JBL GO, `TAPBOX_BT_PAIR=dbus bt.py connect` → paired +
   transport-ready <25s; journal shows zero bluetoothctl pair forks.
2. **Bond survives speaker power cycle** — the non-bonding `pairable`
   gotcha is invisible to the fake; this is the "works today, dead
   tomorrow" one. Power-cycle → btwatchd reconnect <5s, no re-pair.
3. Repeat with JR310BT incl. pair mid-playback (quiesce, resume on new,
   old disconnected).
4. Stale-key repro (forget on Pi only, re-pair) → clear-bond branch
   exactly once (journal).
5. Firmware-crash mid-pair (speaker power pulled at "Connecting"):
   heal <40s, next pair succeeds, `busctl` shows no leftover agent.
6. `gdbus monitor --system --dest org.bluez` during one pair → every
   real error name seen is in the classification table.
7. One full day on `TAPBOX_BT_PAIR=cli` (rollback stays real).

**Incoming (the car):**
1. Box playing to a speaker; PWA "Pair from car" → window opens,
   playback quiesced, btwatchd journal quiet (flock deference).
2. Head unit: scan → "tapbox" appears → pair from the car → completes
   with no interaction on the box. Note which SSP dance the unit used;
   if the box does NOT appear in the car's list → run the Class of
   Device experiment (main.conf `Class=`, deferred item).
3. PWA shows the car; "Use as speaker" → audio in the car. Bond
   survives ignition off/on; **the car reconnects by itself next
   drive** (Trusted path, no agent needed).
4. After the window: `bluetoothctl show` → `Discoverable: no`.
5. Pull power mid-window; after boot `Discoverable: no`; normal
   reconnect works.
6. Box-initiated pair against the car (`bt.py use <carMAC>` with the
   car visible) — the outgoing path against a head unit, not just
   speakers.
7. Adversary check: outside a window a phone cannot see or pair the box.

**Pass criteria for flipping the default:** outgoing 1–6 green on both
speakers + one head unit; bt_pair_dbus 12/12 on the rig; zero
bluetoothctl pair forks over a week of `auto`; one field-week with the
switch flipped on one box before install.sh changes the default.

## 7. Risk ranking (QA)

1. **Radio-op collision during a visible window** (btwatchd paging the
   absent speaker exactly then — guaranteed by the car scenario; the
   documented Zero 2 W firmware crasher, in a car, with a kid). Most
   protective test: visible 7 (flock held) + bt_reconnect 5 (daemon
   defers). If only one new test could be kept, it is this.
2. **Discoverable stuck on** — silent, persistent, security-adjacent.
   visible 2 + 9 (+ D5's bluez-side dead-man switch).
3. **Non-bonding pair over the dbus path** — success at pair time, gone
   next morning; cannot be automated; rig outgoing item 2 is mandatory
   and is exactly why the kill switch defaults to cli.
4. **Legacy-PIN deadlock** — loud and immediate, mitigated by the kill
   switch; pair_dbus 5.

## 8. dbus-python pitfalls (keep visible while implementing)

1. Mainloop attaches at connection creation only (`mainloop=` kwarg);
   never touch the process default loop.
2. `dbus.SystemBus()` is a singleton unless `private=True` — the shared
   loop-less one can never export the agent; this is why `_agent_bus()`
   exists.
3. Nothing blocking between RegisterAgent and loop.quit() except short
   (≤10s) setup calls made before Pair.
4. Explicit `timeout=60` on Pair (25s default silently truncates);
   NoReply → PAIR_ERROR; 75s GLib guard as backstop; track guard-fired
   state (`source_remove` on a fired one-shot warns).
5. Agent method signatures must be exact — a mismatch presents as
   bluez failing the pairing as if the agent were absent.
6. Cleanup ordering: UnregisterAgent → remove_from_connection →
   bus.close(), all best-effort; connection close + DiscoverableTimeout
   are the two guarantees.
7. Trust INSIDE the window + 3s linger — the car's A2DP authorization
   arrives after quit-on-pair would tear the agent down.
8. `Discoverable=true` requires `Powered=true` — `bt_up()` first (also
   rfkill before any bus traffic).
9. Retry paths re-export the agent — safe only because every call
   builds a fresh private connection; never cache the agent connection.
10. `bt_cli()` runs under tapboxd's /usr/bin/python3 (dbus present);
    venv callers degrade: pair → cli fork, visible → exit 2 + message.

## 9. Amendments to PLAN-bt-dbus.md §5 (flagged, applied by this plan)

1. **Strike "reconnect daemon (C) calls RequestDefaultAgent."**
   btwatchd must never host an agent: a permanent default agent is a
   permanently-open incoming-pairing door, contradicts its "no pairing"
   scope, and is unnecessary — reconnects of bonded devices are
   authorized by `Trusted=true`, which `connect()` sets on every pair
   path. `RequestDefaultAgent` lives only in the visible window.
2. **Add:** `DiscoverableTimeout` before `Discoverable=true` as the
   dead-man switch (new requirement; §5 predates the incoming feature).
3. **Note:** outgoing dbus pair timeout is 60s while the cli path uses
   45s — intentional, keep 60 per §2's table.
4. Confirmed unchanged: NoInputNoOutput, callback repertoire,
   `/org/tapbox/agent`, AlreadyExists re-register tolerance.
