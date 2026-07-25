# Remote access & HTTPS — design space and ideas (battery-first)

Working notes for giving the box HTTPS and/or off-LAN access **without
paying an idle-battery tax**. The box is a battery-powered Zero 2 W; idle
hours dominate runtime, so the guiding rule is below. Cross-refs the API
trust models in [`../SECURITY.md`](../SECURITY.md).

## The one principle that decides everything

**Any *always-reachable* tunnel costs periodic keepalives — a wakeup +
radio TX every N seconds, forever.** That is exactly the kind of idle
cost the 2026-07-24 power pass hunted down. So for a battery box the
primitive is not "which tunnel" but:

> **Remote access should be on-demand and default-OFF.** Flip it on when
> you're away and need it; flip it off when you're home. Then the idle
> cost is zero whenever you're not actively using it.

`pi/power.sh` already encodes this instinct: `STOP_TAILSCALE` stops
`tailscaled` in save mode (`power.sh:124-126`) precisely because a
persistent overlay costs battery.

## The design space (brainstormed 2026-07-25)

| Option | HTTPS | Off-LAN access | Idle battery | Who can reach it | Ops |
|---|---|---|---|---|---|
| **LAN-only + API token** | no (http) | no | none | anyone on home Wi-Fi | none |
| **Caddy + Cloudflare DNS-01** | yes, real cert | no (LAN only) | ~none (idle Caddy = no wakeups) | anyone on home Wi-Fi | Caddy + a scoped CF token; local DNS override |
| **Tailscale (managed WG)** | yes, auto | yes | **keepalives** (tailscaled resident) | only tailnet devices | trivial setup, 3rd-party control plane |
| **VPS + WireGuard front** | yes, cert on VPS | yes | **WG keepalive (~25s)**, lighter than TS | anyone (public front) → token MANDATORY | you run a VPS |
| **IPv6 dynamic DNS (direct)** | yes (needs real cert) | yes | **~none (no tunnel/keepalive)** | anyone (internet-facing) → token MANDATORY | DDNS updater; router IPv6 pinhole |

Notes:
- **Caddy + Cloudflare** is the battery winner *if* off-LAN access isn't
  needed: idle Caddy is event-driven (zero wakeups), works fully offline,
  keeps `tailscaled` off. See the HTTPS section of SECURITY.md for the
  LAN name-resolution caveat (rebinding / local DNS override).
- **Tailscale** collapses the whole cert+DNS+resolution stack but every
  device must join the tailnet, and its resident daemon costs idle
  battery — in direct tension with `STOP_TAILSCALE`.
- **VPS + WireGuard** is the self-hosted version of Tailscale: more
  control, no 3rd-party control plane, cert lives on the always-on VPS —
  but the box holds a persistent tunnel (keepalives) and the API becomes
  internet-facing.

## IDEA 1 — on-demand `tailscaled` toggle (screen + PWA), default-off at boot

**Status:** backlog. **Feasibility:** high.

The clean resolution of battery-vs-remote-access. `tailscaled` is
installed but **not enabled at boot** (zero idle cost by default); a
setting flips it on only when wanted, so you get remote access on the
rare away-from-home moment and pay nothing the rest of the time.

Sketch:
- Setting `tailscale_on: (0, 0, 1)` in `sysinfo.py` DEFAULTS (same shape
  as `wifi_ps_bt_off`).
- Privileged endpoint `POST /system/tailscale {enabled: bool}` — same
  shell-to-systemctl pattern as `set_wifi`/`shutdown` (`systemctl
  start/stop tailscaled` + `tailscale up`/`tailscale down`). Gate it
  behind the SECURITY.md token model like the other privileged calls.
- Surfaced in the PWA settings screen and the on-box settings menu (both
  reach the daemon; the on-box screen is the always-trusted path).
- **Boot policy:** always OFF at boot regardless of last state — safest
  for battery (an explicit opt-in per session beats a forgotten toggle
  draining overnight). A "remember last state" variant is possible but
  loses the battery guarantee; default to always-off.

Wrinkles to handle:
- **Don't let it disable itself over its own link.** Turning it OFF from
  a PWA that is *currently reached over the tailnet* would cut the
  connection mid-request. Detect the request's arrival interface/addr and
  refuse (or warn) an OFF toggle that comes in over the tailnet — the LAN
  path can always turn it off safely. Turning it ON from the LAN is
  always safe.
- **First-time auth.** `tailscale up` needs a one-time login (browser
  URL, or a pre-seeded auth key at install). After that the saved node
  state reconnects without re-auth, so the toggle is instant thereafter.

Battery rationale: OFF at boot = the `STOP_TAILSCALE`-in-save-mode cost
never applies unless you asked for it. This is strictly better than
"tailscaled always up."

### Trigger surfaces

- **PWA settings + on-box settings menu (recommended, primary).** The
  right home for a rare, deliberate, parent-only action: discoverable,
  confirmable, and shows the resulting state. The on-box menu already
  covers the "toggle at the box without my phone" case, so no physical
  gesture is needed.
- **PiSugar long-press (considered, advised against).** The PiSugar
  button exposes exactly **three gestures** (single/double/long) and all
  are taken by media controls — single=playpause, double=next, long=prev
  (`power.sh:224-226`). There is no free gesture, so mapping tailscale
  here **sacrifices "previous"**. Worse fit reasons: this toggle has
  battery + security + internet-exposure consequences, so a silent,
  feedback-less gesture a kid can trigger is the wrong home for it (the
  PiSugar button has no display; the only feedback is the Pirate Audio
  screen, often asleep). It's also largely redundant with the on-box
  settings menu. **If** a physical shortcut is still wanted: keep prev on
  the normal long-press, use a distinct *very-long* press, and make it
  **wake the screen and show "Tailscale ON/OFF + hostname"** — never
  silent.

## IDEA 2 — VPS + WireGuard reverse-proxy front

**Status:** backlog. **Feasibility:** solid, well-trodden pattern.

Architecture:
- A small always-on **VPS** holds a real cert for e.g. `tapbox.vibb.me`
  (public DNS A-record → VPS public IP) and runs a reverse proxy
  (Caddy/nginx) that forwards to the box over WireGuard.
- The **box** runs a WireGuard *client* dialling out to the VPS; the VPS
  proxies `tapbox.vibb.me` → `<box WG IP>:3679`.

Why it's attractive:
- Real public HTTPS, works from anywhere — like Tailscale, but **you own
  it** (no 3rd-party control plane / account dependency).
- ACME + cert live on the mains-powered VPS; the **box does no cert work
  at all**.
- WireGuard on Linux is kernel-space — far lighter in CPU/RAM than
  `tailscaled`. For the *always-on* case this is the more
  battery-friendly of the two overlays.

The battery catch (be honest):
- To stay reachable through NAT the box's WG peer needs
  `PersistentKeepalive` (~25s) → a periodic wakeup + tiny TX, forever.
  Lighter than tailscaled, but **not zero**. So for a battery box this
  still wants the same on-demand, default-off treatment (bring the tunnel
  up only when needed — a `wg-quick up/down` toggle mirroring IDEA 1).
- **Internet-facing surface.** The public front means the box's control
  API is reachable from the internet (behind the proxy). The SECURITY.md
  token/auth model stops being optional and becomes **mandatory** — this
  is the biggest difference from every LAN-only option.
- Ops/cost: a VPS (~€3-5/mo) you maintain and keep patched; it is now an
  internet-exposed component.

Relation to Tailscale: this is essentially "self-hosted, unmanaged
Tailscale." Pick it over Tailscale when you want no 3rd-party control
plane and are happy running a VPS; pick Tailscale when you want zero
infra. Both share the keepalive battery cost and therefore both want the
on-demand toggle.

## IDEA 3 — IPv6 dynamic DNS (direct, no tunnel)

**Status:** backlog. **Feasibility:** works *if* three network legs
line up (below). **Battery: the best of the reach-from-anywhere
options — no tunnel, no keepalive.**

Concept: IPv6 gives every device a globally routable address (no NAT), so
the box can be reached *directly* — no relay. The box publishes its
current global IPv6 as an AAAA record (to self-hosted CoreDNS via RFC 2136
dynamic-update / an etcd backend, or — simpler, since we already use
Cloudflare — a tiny DDNS updater against the Cloudflare API). The phone
resolves `tapbox.vibb.me AAAA` → connects directly from anywhere.

Why it's the battery winner: **no persistent tunnel → no keepalives.**
The box only touches the network to *update DNS when its address
changes*, which it can watch event-driven via netlink (≈ free at idle).
Inbound packets arrive fine even with Wi-Fi power save (AP buffers to the
DTIM interval). This is why IPv6 is the right angle — IPv4 at home is
NAT/CGNAT with no inbound path without a relay.

Three hard dependencies (all outside our code, often not all satisfiable):
1. **Working global IPv6 on the box's network** — ISP-dependent (the rig
   is on Telenor; not guaranteed).
2. **An inbound IPv6 firewall pinhole on the home router** for 443 → the
   box. IPv6 has no NAT, but home routers block unsolicited inbound by
   default; without a rule the phone's SYN dies at the router. Not every
   router exposes this, and it must track the box's address. This is the
   real blocker.
3. **IPv6 on the phone's *current* network.** Cellular usually yes (often
   IPv6-only); random Wi-Fi may be IPv4-only → then it literally cannot
   reach an IPv6 literal. Reachability becomes "most places", not
   "always".

Also required:
- Publish a **stable** IPv6 (disable RFC 4941 privacy temp addresses, or
  use an RFC 7217 stable-privacy / token address) — otherwise it rotates
  daily; low AAAA TTL so prefix/address changes propagate fast.
- **Internet-facing surface**, same as IDEA 2: the token gate becomes
  **mandatory** and real HTTPS a requirement; the box's 443 is now
  world-scannable (token-protected, but open).

Tradeoff vs the tunnels: IPv6-DDNS trades **robustness for battery** —
idle-free, but reachability depends on three legs and it's internet-
facing. The tunnels are robust through any NAT / IPv4-only network but
cost keepalives. Natural hybrid: **IPv6-DDNS as the primary idle-free
path, the on-demand tunnel (IDEA 1) as the fallback** for IPv4-only
networks.

## Recommendation snapshot

- **Battery is the priority + access is home-LAN only** → Caddy +
  Cloudflare DNS-01 (idle-free HTTPS), `tailscaled` stays off.
- **Occasionally need off-LAN access** → keep the LAN HTTPS above, and
  add **IDEA 1** (on-demand `tailscaled`, default-off) for the away
  moments. Best of both: zero idle cost, remote access one toggle away.
- **Want a fully self-owned public front** → **IDEA 2** (VPS + WG), also
  on-demand, and ship the token gate first (it becomes mandatory).
- **Battery is sacred AND your ISP/router/phone all do IPv6 reliably** →
  **IDEA 3** (IPv6 DDNS): idle-free direct reach, no tunnel. Ship the
  token gate first; pair with IDEA 1 as the IPv4-only fallback.
