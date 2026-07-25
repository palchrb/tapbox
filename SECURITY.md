# TapBox security notes & TODOs

Living document for the security posture of the existing Pi Zero 2 W box
(the `pi/` tree). Not a disclosure policy — a working list of what the
threat model is, what's exposed, and how we intend to lock down the
open control API without breaking the physical-first UX.

## Threat model

- **Where the box lives:** a home LAN, behind the router's NAT. The
  internet cannot reach it unless someone deliberately port-forwards —
  so the internet is **not** the attack surface. *Other devices on the
  same Wi-Fi are* (guests, kids' friends' phones, a compromised IoT
  gadget, anyone who has the Wi-Fi password).
- **Physical control is trusted by definition.** The screen + the four
  GPIO buttons require holding the box; there is no lockdown to design
  there. The trust problem is entirely the **PWA / HTTP API**, which any
  device on the LAN can reach today.
- **Transient weak moment: hotspot mode.** When the box has no Wi-Fi it
  brings up its own AP + captive portal and exposes the full API to
  anyone who joins.
- **Out of scope (accepted):** a determined attacker who already has the
  Wi-Fi password and can run an active MITM (ARP spoof / rogue AP); a
  physically-present attacker (they can just press the buttons). We are
  defending against *casual* LAN access, not a targeted adversary on
  your own network.

## Current posture / attack surface

| Listener | Bind | Auth | Risk |
|---|---|---|---|
| `tapboxd` API + PWA | `0.0.0.0:3679` | **token on privileged; open on playback/reads** | the main surface — gated since 2026-07-25 |
| `tapboxd` captive portal | `0.0.0.0:80` (`daemon.py:2903`) | none | redirect only; low |
| `go-librespot` API | `localhost:3678` (`install.sh:197`) | none | not LAN-exposed ✓ |
| `pisugar-server` | `127.0.0.1` | n/a | not LAN-exposed ✓ |
| `sshd` | `0.0.0.0:22` | password (distro default) | full compromise — **owner-managed hardening** |
| avahi mDNS | `:5353` | n/a | discovery only |
| hotspot (AP mode) | `10.42.0.1` | WPA2 PSK | default PSK is shipped — see TODO |

Notes:
- **No CORS headers** are sent and there is no `do_OPTIONS`. That is what
  makes the `X-TapBox-Token` header CSRF-proof (a cross-origin page can't
  attach it without a preflight the box never grants) — and it is why
  adding either would silently undo the protection.
- Device control, config and destructive endpoints (`/system/shutdown`,
  `/system/wifi`, `/wifi/*`, `/bt/*`, `PUT /library`,
  `/library/section-logo`, `/output`, `/spotify/logout`) now require the
  token. Playback and reads stay open by design — see the classification
  below.

## Endpoint classification (the basis for the split below)

**Safe / annoyance-tier** — worst case is a LAN prankster pausing the
kid's music. Keep these open so the zero-setup "Hey Siri, pause TapBox"
shortcut keeps working:
- `GET /status`, `/system`, `/settings`, `/library`, `/artwork`
- `POST /playpause`, `/next`, `/prev`, `/pause`, `/volume`, `/shuffle`,
  `/stop`

**Privileged** — device control, config, or destructive. These are what
we want behind a trust gate:
- `POST /system/shutdown`, `/system/wifi`
- `POST /wifi/connect|forget|add|reconnect|hotspot|scan`
- `POST /spotify/logout`
- `PUT /library`, `POST /library/section-logo`, `POST /settings`
- `POST /bt/scan|pair|connect|forget|rename|visible`
- `POST /output`

(`/bt/lost` is an internal btwatchd → daemon call and is token-gated
like the rest; btwatchd authenticates through `boxapi`.)

## Done

- **2026-07-25 — the API gate is LIVE (Model A+B, 5 commits).** Privileged
  endpoints require the box token; playback and reads stay open so the
  phone shortcut ("Hey Siri, pause TapBox") needs no setup. Provisioned
  by scanning a QR on the box screen (Settings → Link phone); the token
  rides in the URL fragment so it never reaches a server log. Recovery:
  the same screen re-displays and rotates it, `install.sh` prints it, and
  `sudo cat /etc/tapbox/api-token` is the SSH fallback. `/play` is split
  by body — a library `id` stays open, a raw `target` needs the token.
  Internal callers (ui, btwatchd) authenticate via the token file in
  `boxapi.py`, deliberately not a localhost bypass. Covered by
  `tests/api_token.py`, `api_auth_gate.py`, `pwa_token.py`,
  `ui_link_phone.py`, `install_token.py`.

- **2026-07-25 — `Content-Type: application/json` required on POST/PUT.**
  Closed a *live* CSRF hole: `do_POST` swallowed a JSON `ValueError` and
  continued with `body = {}`, so a plain auto-submitting `<form>` on any
  page a LAN user opened could fire every bodyless endpoint
  (`/system/shutdown`, `/wifi/reconnect`, `/bt/scan`, `/spotify/logout`,
  `/stop`). Cross-site simple requests can't set `application/json`, and
  anything else needs a preflight this server never grants.
  `tests/api_csrf_content_type.py` also pins the two assumptions it rests
  on: **no `Access-Control-*` headers, no `do_OPTIONS`.** Adding either
  silently re-opens the hole.

## TODOs — ranked

1. ~~**Split safe vs privileged and gate the privileged set**~~ —
   **DONE 2026-07-25** (see Done). Removed "fully control / brick the
   device" from the open surface while keeping playback zero-auth.
2. **SSH hardening** — key-only auth, ideally LAN-restricted. The only
   full-compromise path. *Owner-managed (handled outside this repo).*
3. **Per-box hotspot PSK.** `HOTSPOT_PSK` defaults to the shipped
   constant `"tapbox123"` (`pi/tapbox/netmgmt.py:108`). Generate a
   per-box secret at install (`TAPBOX_HOTSPOT_PSK`) and show it on the
   screen when the hotspot is up.
4. ~~**Gate `/bt/lost` and other internal-only endpoints**~~ — done via
   the token gate (btwatchd authenticates through `boxapi`).
5. **Optional: UFW default-deny inbound + allowlist** (`22, 3679, 80,
   5353` + hotspot ports). Defense-in-depth only — it can't close
   `:3679` (the PWA needs it), so it mainly stops a *future* accidental
   service from being exposed. Modest value on a single home LAN.

## API trust models for the PWA

> HTTPS and off-LAN access options (Caddy + Cloudflare, Tailscale,
> VPS + WireGuard) and the battery tradeoffs are worked out separately in
> [`docs/remote-access.md`](docs/remote-access.md). The token model below
> is what protects the API *regardless* of transport.

Design anchor: **the box has a screen.** That makes "prove you physically
hold the box" cheap — a PIN or QR shown on the display is a natural,
strong-enough capability handoff for a home device. Everything below
assumes the safe/privileged split from TODO #1, so playback stays open
and only the privileged set needs a credential.

### Model A — Safe/privileged split, no credential (do this first regardless)

Just classify and refuse the privileged set unless a token is present.
Even before any provisioning UX exists, this converts the box from
"anyone on Wi-Fi can brick it" to "anyone on Wi-Fi can pause the music."
Low effort, and it's the foundation for A+B/A+C.

### Model B — Shared per-box token, provisioned from the screen (recommended)

- Box generates a random token at first boot, stored `/etc/tapbox/
  api-token` (mode 0600, root/daemon only).
- Privileged endpoints require it in a **custom header**
  (`X-TapBox-Token: …`). Custom header, not a cookie: a cookie would need
  `SameSite` to resist CSRF, whereas a custom header can't be set
  cross-origin without a CORS preflight the box never grants — so this
  defeats CSRF for free.
- **Provisioning:** Settings → "Link this phone" shows a **QR** encoding
  `http://<box>:3679/#token=<token>`; the PWA reads it, stores the token
  in `localStorage`, and sends it on every privileged call. A short
  numeric PIN typed into the PWA is the fallback for a phone that can't
  scan.
- **Revocation:** regenerate the token from the screen — every linked
  phone is logged out at once. (Good enough; per-device revocation is
  Model C.)
- **Cost:** one middleware check on the box, a QR lib + localStorage in
  the PWA, one Settings screen. Small.

### Model C — Per-device tokens with on-screen approval (TOFU)

Like B, but each phone gets its own token and a new device must be
**approved on the box screen** ("Allow this phone? [A]=yes") the first
time. Gives per-device revocation and an audit of who's linked. More
code (a device table, an approval flow on the screen); worth it only if
the box will live on shared/untrusted networks.

### Model D — Network-level (not recommended as the primary control)

Binding privileged endpoints to a fixed IP/subnet, or an admin VLAN.
Fragile against DHCP and doesn't fit "the parent's phone, anywhere on
the LAN." Fine as an *extra* layer, wrong as the main gate.

### Model E — WebAuthn / passkeys (the strong-auth upgrade, for the internet-facing case)

Possible, and elegant, but **downstream of HTTPS** — `navigator.
credentials` only runs in a secure context, and the credential binds to an
RP ID = the domain (so a real domain beats `.local`; passkeys sync poorly
for a `.local` RP). Assessment:

- **For the LAN threat it's over-engineering.** Model B (token/PIN)
  already closes "anyone on the Wi-Fi can control the box" with a fraction
  of the code and *no HTTPS prerequisite*. WebAuthn defends against
  phishing / credential theft at scale — threats a single home appliance
  on the LAN doesn't really face.
- **It earns its complexity when the box goes internet-facing** (remote
  access IDEAs 2/3 in `docs/remote-access.md`): there a leaked bearer
  token is game-over, whereas a passkey **cannot be exfiltrated** (private
  key stays in the device's secure enclave) and is phishing-resistant.
  HTTPS is already mandatory in that scenario, so the prerequisite is met.
- **Daily UX is low-friction** (Face ID tap); the cost is server-side
  implementation (registration/assertion ceremonies, a credential store,
  a session layer) and enrollment/recovery design. The **box screen is
  the enrollment/recovery anchor** ("approve this passkey on the box"),
  same physical-possession trick as the token QR. Note that after a
  successful assertion you still issue a session token — WebAuthn just
  replaces *how the session is obtained* with something stronger.

Layering: ship **Model B now** (LAN), keep **Model E as the phase-2
upgrade** if/when the box becomes internet-facing.

### Transport caveat (be honest about it)

All of the above send the token over **plain HTTP**. On a home WPA2
network that is meaningfully protected — per-station unicast is
encrypted, so another Wi-Fi client can't passively sniff the token
without your Wi-Fi password *and* an active MITM. It is **not** safe on
an open/untrusted network or in hotspot mode. Closing that fully means
HTTPS, which on a `.local` box means a self-signed cert and browser
trust friction — deferred, and only needed if the box must be trusted on
networks you don't control. For the home threat model, token-over-HTTP is
the right stopping point.

### Decided implementation (architect + QA reviewed, 2026-07-25)

**Model A + B — SHIPPED 2026-07-25.** Kept here as the design record;
every blocking item below was implemented and is covered by a test.

Owner decisions:
- **QR on the box screen is the primary provisioning route.** Every box
  in the fleet now has a screen, so the "headless box has no way in"
  problem is out of scope by construction.
- **Fallback is SSH:** `sudo cat /etc/tapbox/api-token`. `install.sh`
  also prints the token and the link at the end of a run.
- QR encodes the box's **stable `<name>.local`**
  (`http://tapbox.local:3679/#t=<TOKEN>`), not its IP. The browser keeps
  the token per ORIGIN, so an IP-based link dies the moment DHCP moves
  the box or it comes up as its own hotspot — you'd re-scan every time.
  The name resolves in BOTH modes: mDNS on the LAN, and in hotspot the
  captive resolver answers every name with the box
  (`address=/#/10.42.0.1`). The IP is printed under the QR for the rare
  client that can't resolve mDNS: browse there and paste the token.
- Token in the URL **fragment** (`#t=`), never the path: fragments are
  never sent to the server, so the secret can't land in a log or Referer.
- Token is **Crockford base32, 16 chars** — one secret that is both
  QR-encodable and typeable (`XXXX-XXXX-XXXX-XXXX`). No separate PIN
  mechanism (a PIN would need a new *unauthenticated* claim endpoint).

Blocking items from the QA review — all **done**:
1. ~~Require `Content-Type: application/json`~~ — done (see Done).
2. ~~`install.sh` prints the token + link; document the SSH fallback~~ —
   done, on every exit path.
3. ~~**Fail-closed token rules:** unreadable/missing token file ⇒ deny;
   **empty or short token ⇒ deny** (`hmac.compare_digest("", "")` is
   `True`, so a truncated file would authorize everyone); `ensure()`
   creates only when absent and must never rewrite on a transient error
   (that would unlink every phone)~~ — done, `tests/api_token.py`.
4. ~~**Label-based settings dispatch in `pi/ui.py`**~~ — done; every row
   is walked in `tests/ui_link_phone.py`, and `ui_hotspot.py`'s index
   pins were converted too.
5. ~~Add `qrcode` to install.sh's venv **import probe**~~ — done.
6. ~~Gate design: **default-deny.** An explicit SAFE allowlist
   (`GET`/`HEAD` blanket-safe + the playback POSTs); every unknown
   method or path is privileged. Auto-wrap all `do_*` at import so a
   future `do_DELETE` is closed without anyone remembering. The blanket
   `GET: True` rule is structural — static files and `<img>` artwork
   loads can't carry a header — so **no privileged endpoint may ever be
   a GET.**
7. Internal callers use the **token file via `boxapi.py`** (one line,
   covers `ui.py` and `btwatchd.py`), **not** a localhost bypass: a
   future Caddy reverse proxy would make every request look local and
   silently open everything. `play.sh` needs the header too once `/play`
   with a raw `target` becomes privileged.
8. Corrected from the original plan: **no install.sh restart reorder is
   needed** — the order is already btwatchd → daemon → UI.

Related follow-up worth the same pass: **`/play` with a raw `target`
should become privileged** (`/play` with `id` stays open). It makes the
box fetch an arbitrary URL via mpv/yt-dlp, which is a bigger blast radius
than "pausing the music". The Content-Type fix already removes the
drive-by vector; this closes the LAN-local one.

### Recommended path

**A + B**: split the endpoints, gate the privileged set behind a per-box
token, provision it with a screen QR (PIN fallback). It kills the
"brick/hijack the box from any LAN device" capability *and* the CSRF
vector, keeps the pause-from-phone shortcut zero-setup, and leans on the
screen the box already has. Escalate to **C** (per-device + on-screen
approval) only if the box starts living on untrusted networks; add
**HTTPS** only alongside that.

## Non-goals / accepted risks (current)

- Playback control (pause/next/volume) stays open on the LAN by design —
  a prankster pausing music is an acceptable worst case, and it keeps the
  Siri/Shortcut pause working with no setup.
- We are not defending against someone who already has the Wi-Fi
  password and runs an active MITM, nor against physical access.
- The box is assumed to be behind home NAT. **Do not port-forward
  `:3679`, `:22`, or `:80` to the internet** — none of them are built for
  that exposure.
