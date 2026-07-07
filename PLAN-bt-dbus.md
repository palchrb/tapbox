# Plan: bt.py step 2 — BlueZ D-Bus port

Status: **in progress** — A0 landed; A1 parity PASSED on the rig 2026-07-07 (`auto` prefers dbus for reads); B1 actions implemented + gated by tests/bt_actions.py, field-verified (speaker switching over dbus). C implemented (pi/btwatchd.py event daemon replaces the bash poll loop; bash kept as tapbox-bt-reconnect-poll fallback) — gate with tests/bt_reconnect.py ON THE RIG before relying on it. Pairing stays cli until B2. Remaining: rig-run the C gate + <5s power-on reconnect check, then B2 (Agent1), then D (cleanup). Drafted 2026-07-07 and refined by three
review passes (architecture, implementation, test) against the codebase
as of commit `c379b01`. This document is the implementation bible; the
reviews' full findings are folded in below.

## 1. Goal & scope

Replace the `bluetoothctl` text-parsing transport (~19 parse sites in
`pi/tapbox/bt.py`) and the `bluealsa-aplay -L` fork in `audio_ready()`
with direct D-Bus (org.bluez + org.bluealsa), and replace the bash
`tapbox-bt-reconnect` poll loop with an event-driven daemon.

Amendments accepted from review:

- **RSSI is for sorting/display only.** RSSI on unpaired devices is
  noisy and auto-pairing "nearest by signal" can grab a neighbour's
  speaker. `pair_auto()` keeps its exactly-one-candidate safety rule.
- **The bluetoothctl backend is never deleted.** It is the escape hatch
  for packaging/firmware variance (same reasoning as the
  bluealsa/bluealsad unit-name handling). Phase D deletes duplicated
  flow logic (there should be none), not the fallback transport.
- **The reconnect daemon takes no part in crash recovery.** Three
  actors can already trigger `ensure`/`recover()` (stall watchdog,
  player's racing guard, PWA); a fourth restarting bluetoothd is how
  the racing-guard class of bugs comes back. It stays passive on
  adapter loss.

Out of scope: recover()/_hci_crashed()/serdev re-probe (stay
subprocess/sysfs/journal — BlueZ's view is stale after a firmware
crash, recorded in bt.py docstrings), rfkill handling (must run
*before* any D-Bus call: `Powered=true` fails outright while
soft-blocked), the PWA/API surface (unchanged).

## 2. Architecture decisions

**Library:** `python3-dbus` (dbus-python) for all bus work +
`python3-gi` (GLib loop) only where signals or an exported object are
needed. Both apt-installable, preinstalled on RPi OS. Rejected: pydbus
(unmaintained), jeepney/dasbus (no/roundabout object export for
Agent1), raw sockets (not worth it).

**Mainloop placement (key decision):**

| Process | Loop? | Why |
|---|---|---|
| tapboxd (imports bt.py) | **never** | blocking calls only (`GetManagedObjects` per bt_status); preserves the threading model exactly |
| bt.py CLI subprocess | only during `pair` | Agent1 export needs dispatch; short-lived process, no leak surface |
| tapbox-bt-reconnect | yes (the only long-lived loop) | signal subscriptions |

`DBusGMainLoop(set_as_default=True)` must run before bus acquisition
(classic gotcha). Footprint: the Python reconnect daemon costs ~20-25MB
RSS vs ~1-2MB bash, but kills the 1-3 bluetoothctl forks/min (~10MB
each, themselves D-Bus clients); net wakeups drop sharply — the metric
that matters on battery.

**Module structure:** new **`tapbox/btbus.py`** transport layer with
two backends behind one narrow primitive surface, chosen once at
process start (`TAPBOX_BT_BACKEND=cli|dbus|auto`, default `auto`: try
dbus, fall back to cli on import/bus failure, log which):

```
adapter_up() / set_pairable()      device_info(mac)
discover(secs) -> [{mac,name,audio,rssi}]
pair(mac) -> OK|ALREADY_EXISTS|AUTH_FAILED|NOT_AVAILABLE|ERROR
trust(mac)   connect_profile(mac)   disconnect(mac)   remove(mac)
connected_devices()   a2dp_pcm_present(mac)
```

**Error classification is the transport contract.** The dbus backend
maps typed `org.bluez.Error.*` names; the cli backend keeps today's
regexes. bt.py's `connect()` flow — info retry, pair classification,
stale-key clear-and-retry-once, 3-attempt connect, `_hci_crashed()`
pre/post checks, PCM wait, MAC_FILE write, `_route_alsa`,
`_disconnect_others` — exists in exactly one copy and never mentions
the backend. MAC_FILE/ASOUND/one-output policy are policy, not
transport; they stay in bt.py.

**Lazy bus init is mandatory:** importing `tapbox.bt` must not open a
bus (venv processes without dbus bindings, test environments without a
bus, daemon that only wants bt_status). One-shot paths build proxies
fresh per call and cache nothing — bluetoothd restarts become a
non-event. Explicit per-call timeouts mirroring today's CLI budgets
(Pair 60s, Connect 30s; dbus-python's 25s default silently truncates).
`ServiceUnknown`/`NoReply` map to ordinary classified failures so
`_hci_crashed() -> recover()` fires on today's schedule, and
`bt_status()` maps bus-down to the same empty result as today (the /bt
endpoint must not start throwing during firmware crashes, exactly when
the UI polls hardest).

**Cross-process lock:** BlueZ's own serialization is NOT enough — it
returns `InProgress` per device but will happily start an A2DP connect
to speaker B while a pair of speaker A runs (the documented firmware
crasher). `BT_LOCK` is a threading.Lock, invisible across processes,
and Phase C adds a second long-lived BT actor. Add **flock on
`/run/tapbox/bt.lock`**: bt.py CLI `main()` takes it for
connect/pair/forget/ensure/recover; the reconnect daemon tries LOCK_NB
before any connect and re-arms its timer if held. flock auto-releases
on process death; ~10 lines. tapboxd does not hold it — the bt.py
subprocess is the process doing radio work.

## 3. Phases (revised cut)

- **A0 — the seam, no D-Bus.** Extract btbus.py with the *cli* backend
  only + `TAPBOX_BT_BACKEND` env. Pure refactor; existing E2E repros
  (racing guard, heal) must pass unchanged with `cli` exported. This is
  the real risk reducer.
- **A1 — read-only D-Bus.** bt_status()/discover()/audio_ready() via
  GetManagedObjects. Gate: cli/dbus parity diff on the same rig state.
- **B1 — agent-less actions.** connect/trust/disconnect/remove
  (`Device1.Connect` — NOT ConnectProfile; `Trusted=true`;
  `Adapter1.RemoveDevice`). None need an agent.
- **B2 — pair + Agent1.** **The single riskiest step**: bluetoothctl's
  built-in agent auto-accepts today; a self-registered NoInputNoOutput
  agent subtly changes the SSP dance. Kill switch defaults to `cli`
  until B2 passes the rig matrix.
- **C — event-driven reconnect daemon.** Needs only A0+B1 (it never
  pairs) and may land before B2. Rollback: keep the bash script in
  install.sh behind a variable for one release.
- **D — cleanup.** Delete duplicated flow logic; keep both transports
  and the bluealsa-aplay fallback through at least a season of field
  use.

## 4. Operation mapping (cli -> D-Bus)

System bus, service `org.bluez`. Device path =
`/org/bluez/hci0/dev_` + `mac.upper().replace(":", "_")`.

| Today | D-Bus | Errors replacing regexes |
|---|---|---|
| `power on` | `Adapter1.Powered=true` (Properties.Set on /org/bluez/hci0) | `.Failed` ("Blocked through rfkill" — keep rfkill unblock subprocess first); `ServiceUnknown` = bluetoothd down |
| `pairable on` | `Adapter1.Pairable=true` | — (bonding-pairing gotcha: must be set exactly as bt_up() does today) |
| `show` "Powered: yes" | `Adapter1.Powered` Get; adapter absent in GetManagedObjects = not-ok | crash detection itself stays hciconfig/journal |
| `devices Paired` (+ `paired-devices` fallback) | GetManagedObjects, Device1 where `Paired==true` | old-bluez fallback branch dies |
| `devices Connected` | same, `Connected==true` | — |
| `info <mac>` | `Properties.GetAll` on dev path — use `Alias` (always present), not `Name` (optional); `Icon` optional; `.get()` everything | `UnknownObject` = today's "not available", and absence is authoritative (info-retry loop shrinks) |
| `scan on` + `[NEW] Device` regex | `SetDiscoveryFilter({"Transport":"bredr"})` -> `StartDiscovery()` -> collect InterfacesAdded/PropertiesChanged for N secs -> `StopDiscovery()` | `.InProgress` (another client — proceed, filter didn't apply), `.NotReady`, `.Failed` |
| `pair` | `Device1.Pair()` | `.AlreadyExists` -> continue; `.AuthenticationFailed/.AuthenticationCanceled` (+ deliberately add `.AuthenticationRejected/.AuthenticationTimeout`) -> stale-key branch; `.ConnectionAttemptFailed`/UnknownObject -> "not available"; `.InProgress`/`.Failed` -> generic |
| `trust` | `Device1.Trusted=true` — after pair success AND on the AlreadyExists path (today's code skips trust there; fix in the port) | — |
| `connect` | `Device1.Connect()` | `.AlreadyConnected` -> success; `.NotConnected` on Disconnect -> success; `.Failed` carries detail msgs since 5.62 (log, don't parse); keep 3-attempt/3s loop |
| `remove` | `Adapter1.RemoveDevice(o)` | missing object -> tolerated as today |

Discovery details: subscribe before StartDiscovery; list only devices
seen THIS window (GetManagedObjects alone returns bluez's cache of
long-gone devices — ghost-picker trap). Sort `get("RSSI", -999)`. Audio
heuristic port: `Icon.startswith("audio")` OR AudioSink UUID
`0000110b-...` in `UUIDs` (lowercase) — plus a genuine improvement:
`Class` major device class 0x04 (`(Class >> 8) & 0x1F == 0x04`) works
pre-SDP when UUIDs are still empty. Discovery auto-stops when the bus
connection closes (a crashing CLI can't leak a scan).

## 5. Pairing agent (Agent1) — B2

- Export agent at `/org/tapbox/agent`; register via
  `org.bluez.AgentManager1` at path **`/org/bluez`**:
  `RegisterAgent(path, "NoInputNoOutput")`. `AlreadyExists` on
  re-register: unregister-then-register or ignore.
- Outgoing `Pair()` uses the agent registered by the SAME bus
  connection; `RequestDefaultAgent` is only needed for incoming
  (speaker-initiated) authorization — CLI skips it, reconnect daemon
  (C) calls it.
- Callbacks: `Release`/`Cancel` no-ops; `AuthorizeService` -> allow;
  `RequestConfirmation` -> allow; `RequestPinCode` -> "0000",
  `RequestPasskey` -> 0 (legacy speakers). JBL GO/JR310BT negotiate
  Just-Works: no callback fires at all. Reject = raise
  `org.bluez.Error.Rejected`.
- **Deadlock trap:** agent methods are incoming calls on our
  connection; a blocking `Pair()` on the same connection starves them
  on any legacy-PIN device. Run the GLib loop and call Pair async (or
  in a worker thread), `timeout=60`.

## 6. bluealsa D-Bus (audio_ready)

Service `org.bluealsa`, root `/org/bluealsa` (ObjectManager). PCM
objects `/org/bluealsa/hci0/dev_<MAC_>/<profile>/<mode>` — ours is
`a2dpsrc/sink`, interface `org.bluealsa.PCM1`. "Ready" = the PCM object
for the MAC exists with `Mode=="sink"` (presence is the signal; don't
depend on `Running`, newer bluez-alsa only). Keep the
`bluealsa-aplay -L` fallback permanently.

Verify on the rig before coding (exact path grammar + D-Bus policy +
name suffix):

```
busctl --system tree org.bluealsa            # with speaker connected
busctl --system introspect org.bluealsa /org/bluealsa/hci0/dev_<MAC>/a2dpsrc/sink
cat /usr/share/dbus-1/system.d/bluealsa.conf  # caller policy (root ok)
systemctl cat bluealsa bluealsad              # --dbus=SUFFIX changes the name
busctl --system introspect org.bluez /org/bluez/hci0
gdbus monitor --system --dest org.bluez       # during a manual pair: real error names
```

## 7. Reconnect daemon (C)

Python + GLib loop, states as event-driven equivalents of the bash
loop:

- **BOOT**: wait for org.bluez name owner + `Powered=True`
  (PropertiesChanged), attempt immediately; 5s retry timer bounded to a
  ~120s window (replaces `SECONDS<120`, including the re-`power on`).
- **STEADY**: target `Connected=True` — zero timers, pure signal wait.
- **WAITING**: attempt on InterfacesAdded/PropertiesChanged for the
  target path; GLib backoff timer 20->300s re-armed on events. Gio file
  monitor on `/etc/tapbox/bt-headset` for instant retarget.
- **NO_TARGET**: idle on the file monitor only.

Name-owner watch on org.bluez (and org.bluealsa): owner lost -> drop
proxies/matches, go quiet; owner gained -> re-add matches
unconditionally (idempotent), re-read adapter state, re-enter BOOT
fast-window — this makes `recover()`'s bluetooth restart automatically
produce a fast reconnect. No recovery role (see §1). Takes the flock
LOCK_NB before any connect.

**Flagged product decision:** event-driven one-output enforcement
(auto-disconnect a non-active device the moment it self-connects) is
NEW behavior — today `_disconnect_others` runs only inside `connect()`.
Probably right, but decide explicitly and debounce it (a kicked headset
that auto-reconnects would loop).

## 8. Test strategy

**Build order (test-driven):**
1. A0 seam in the all-cli tree; re-run racing-guard + heal E2E repros
   with `TAPBOX_BT_BACKEND=cli` — must pass byte-identical.
2. `fake-bluezd.py` harness (~150 lines): python3-dbus service on a
   private `dbus-daemon --print-address` bus, exporting /org/bluez
   (ObjectManager + Adapter1 + Device1 + agent-caller) and /org/bluealsa
   (PCM1), plus a control interface `org.tapbox.Mock`: AddDevice,
   SetPairResult("auth-failed"|...), CrashAdapter, DropName,
   EmitConnected. Matches the repo's process-level fake style; no
   pytest. (python3-dbusmock's bluez5 template rejected as primary: no
   Agent1 callbacks, no error repertoire, no RSSI/mid-scan appearance,
   no org.bluealsa, no NameOwnerChanged.)
3. **Parity gate for A1**: bt_status()/scan-raw under cli (PATH fakes)
   vs dbus (fake service) — diff the JSON.
4. Every scenario runs twice (`for be in cli dbus`); one dedicated test
   runs `auto` with no bus at all and asserts the cli fallback + its
   log line.

Bus injection: standard `DBUS_SYSTEM_BUS_ADDRESS` (dbus-python honors
it). Verify the daemon unit doesn't scrub env; if it does, add
`TAPBOX_DBUS_ADDRESS` read before SystemBus(). PATH fakes for
hciconfig/journalctl/systemctl keep intercepting under both backends
(recovery stays subprocess).

**Regression traps with tests that catch them:**

| Trap | Test |
|---|---|
| BT_LOCK per-process; C adds a 2nd BT actor | fake-bluezd logs call order; EmitConnected during a paired connect -> assert no overlapping Connect + flock demanded |
| dbus backend connecting at import | `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent python3 -c "from tapbox import bt; bt.bt_status()"` must succeed via fallback |
| recover() kills proxies / daemon goes deaf | heal E2E: fake systemctl respawns fake-bluezd under a new name owner; bt_status works after, EmitConnected still triggers reconnect |
| Boot fast-window lost | fake rejects Connect for first N seconds; assert <=5s retries inside window, backoff only after |
| Quiesce ordering | timestamps in fake-mpv + fake-bluezd logs: StartDiscovery only after mpv stop |
| Pair-error classification | SetPairResult trio: AlreadyExists->continue, AuthenticationFailed->remove+repair once, Failed->not-seen message |
| `_hci_crashed()` precedence | heal repro (<40s wall) under dbus backend with fake-bluezd reporting Powered=yes while hciconfig says DOWN |

**Rig checklists (manual):**

- *A*: scan-raw parity vs cli; PWA picker identical + quiesce logged;
  bt_status parity with 2 speakers; audio_ready flips <2s on connect
  AND works with bluealsa name absent; `systemctl stop bluetooth` ->
  graceful degrade, auto-recover on start.
- *B*: factory-reset JBL GO fresh pair <25s to transport-ready, bond
  survives speaker power cycle; pair JR310BT mid-playback (quiesce,
  resume on new, old disconnected); stale-key repro (forget on Pi only,
  re-pair) hits the clear-bond path; charger-pull heal **<40s** from
  crash line to "audio output is back"; forget/disconnect from PWA incl.
  active-device MAC_FILE clear; one full day on `TAPBOX_BT_BACKEND=cli`
  (rollback path is real).
- *C*: reboot reconnect <30s, no Host-is-down spam; speaker off->on
  reconnect **<5s** (vs <=60s poll today); 30 min away -> backoff
  cadence >= old, radio quiet; old speaker self-connects while new
  active -> kicked <5s (debounced); `systemctl restart bluetooth` under
  the daemon -> resubscribes, next reconnect still <5s; charger-pull
  heal with daemon running -> still <40s, exactly one "Connecting
  (A2DP)" per heal.

## 9. Top implementation pitfalls (from review, keep visible)

1. Blocking Pair() vs agent dispatch deadlock -> async Pair + loop.
2. dbus-python 25s default timeout silently truncates Pair/Connect.
3. GetManagedObjects returns bluez's cache -> gate scan lists on this
   window's events.
4. Never cache proxies across recover(); per-call resolution.
5. `Name` optional -> use `Alias`; `.get()` everything.
6. `AlreadyConnected`/`NotConnected` are successes — map before the
   generic-fail branch.
7. StartDiscovery `InProgress` non-fatal; StopDiscovery only after own
   successful start (refcounted per client).
8. Set `Trusted=true` also on the AlreadyExists pair path (existing
   gap).
9. venv/python interop: pin `bt_cli()` argv to /usr/bin/python3 (or
   venv with --system-site-packages) so dbus imports resolve.
10. rfkill unblock + crash detection stay subprocess/sysfs and run
    BEFORE any D-Bus call.

## 10. Rollout & rollback

- install.sh: `apt install python3-dbus python3-gi` (likely present);
  new unit for the Python reconnect daemon in C, bash loop kept behind
  a variable for one release.
- Kill switch at every stage: `TAPBOX_BT_BACKEND=cli` in
  `/etc/tapbox/rfid.conf`-style env file or the unit — documented in
  README.
- Success metrics: speaker power-on reconnect <5s (from <=60s);
  charger-pull heal <=40s (no regression from 19-22s baseline + real
  hw margin); zero bluetoothctl forks in steady state; journal free of
  parse-related misclassifications.
