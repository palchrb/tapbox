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
| `tapboxd` API + PWA | `0.0.0.0:3679` (`daemon.py:164`) | **none** (`daemon.py:162-163`) | **the main surface** — see endpoint table |
| `tapboxd` captive portal | `0.0.0.0:80` (`daemon.py:2903`) | none | redirect only; low |
| `go-librespot` API | `localhost:3678` (`install.sh:197`) | none | not LAN-exposed ✓ |
| `pisugar-server` | `127.0.0.1` | n/a | not LAN-exposed ✓ |
| `sshd` | `0.0.0.0:22` | password (distro default) | full compromise — **owner-managed hardening** |
| avahi mDNS | `:5353` | n/a | discovery only |
| hotspot (AP mode) | `10.42.0.1` | WPA2 PSK | default PSK is shipped — see TODO |

Notes that make the API risk concrete:
- **No CORS headers** are sent (`_send_unsafe`, `daemon.py`), so browsers
  block cross-origin *reads* — but a malicious page the parent visits
  can still fire cross-origin **state-changing POSTs** (CSRF) at
  `tapbox.local:3679`. A token requirement (below) closes this too.
- The API can **fully control and brick the box** from the open surface:
  `/system/shutdown`, `/system/wifi`, `/wifi/connect` (move the box to
  an attacker's network), `/spotify/logout`, `PUT /library` (wipe the
  whole library), `/library/section-logo` (3 MB base64 upload), the
  `/bt/*` family, `/output`, `/stop`.

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

(`/bt/lost` is an internal btwatchd → daemon call; it should be bound to
localhost or token-gated like the rest, not left open.)

## TODOs — ranked

1. **Split safe vs privileged and gate the privileged set** (below). This
   is the highest-value, lowest-effort change: it removes "fully control
   / brick the device" from the open surface while keeping playback (and
   the pause shortcut) zero-auth. **Directly addresses the owner's
   concern.**
2. **SSH hardening** — key-only auth, ideally LAN-restricted. The only
   full-compromise path. *Owner-managed (handled outside this repo).*
3. **Per-box hotspot PSK.** `HOTSPOT_PSK` defaults to the shipped
   constant `"tapbox123"` (`pi/tapbox/netmgmt.py:108`). Generate a
   per-box secret at install (`TAPBOX_HOTSPOT_PSK`) and show it on the
   screen when the hotspot is up.
4. **Bind `/bt/lost` (and any other internal-only endpoint) to
   localhost.**
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
