#!/usr/bin/env python3
"""tapbox-snoop-digest — answer questions a btsnoop grep can't.

The ring (tapbox-btsnoop) already settled WHO crashes: the chip reports
Hardware Error itself mid-clean-traffic (btsnoop analysis 2026-07-27).
This tool is for the NEXT question: what was the radio doing in the
seconds before each crash. Field journal 2026-07-27 planted a concrete
hypothesis — both heal-able crashes (20:05:09, 20:08:27) landed 10-25s
after an A2DP pause/resume cycle (bluealsa 'Pausing IO thread' →
'resumed'), i.e. AVDTP Suspend/Start churn during AVRCP chatter, the
known channel-ops-while-streaming pattern. The digest extracts, per
Hardware Error anchor:

  - the last control-plane packets before it (audio payload skipped)
  - time since the last AVDTP Start/Suspend and the last AVRCP PDU
  - AVRCP volume in the final 10s vs the whole capture

plus capture-wide histograms (AVRCP PDU mix, AVDTP ops, HCI commands)
so two captures can be compared — e.g. a JBL trip against a Skoda trip,
or before/after the mpris player registration.

Usage (on the box, or anywhere btmon exists):
    tapbox-snoop-digest ~/20260727-200749.snoop [more.snoop ...]
    btmon -r x.snoop | tapbox-snoop-digest -      # pre-rendered text ok

Stdlib only; .snoop files are rendered through `btmon -r`.
"""

import re
import subprocess
import sys

# packet header:  "> HCI Event: Hardware Error (0x10) plen 1  #7 [hci0] 38.76"
HDR = re.compile(r"^([<>])\s+(.+?)\s+#\d+\s+\[hci\d\]\s+(\d+\.\d+)\s*$")
AVRCP = re.compile(r"AVRCP:\s+([A-Za-z][A-Za-z ]*?)(?:\s+pt\s|\s*[:(]|\s*$)")
AVDTP = re.compile(r"AVDTP:\s+(\w+)\s*\((0x[0-9a-f]+)\)\s*(\w+)?")
L2CAP = re.compile(r"L2CAP:\s+([A-Za-z][A-Za-z ]*?)\s*\(")
ERR = re.compile(r"Error:\s+0x[0-9a-f]+\s+\(([^)]+)\)")
# per-audio-packet bookkeeping, useless in a control-plane digest
NOISE_EVENTS = ("Number of Completed Packets",)

TAIL_N = 30        # control packets shown before each crash
NEAR_S = 10.0      # "final seconds" window for rate comparison
# Link-policy events — the baseband things a peer can do to a live ACL
# (sniff mode, role switch, packet-type renegotiation) that combo-chip
# firmware is notoriously sensitive to. Became the prime suspect when
# the pre-mpris capture crashed at a CALM 2 AVRCP PDU/s (2026-07-27):
# the storm theory couldn't explain it, so what differs between the
# Skoda and the JBL must live at this layer.
LINK_EVT = ("Mode Change", "Role Change", "Connection Packet Type",
            "Max Slots Change", "QoS Setup", "Encryption Change",
            "Link Supervision", "Sniff Subrating")


def classify(hdr, sublines):
    """One short tag for a packet, or None for audio/bookkeeping."""
    direction, desc = hdr
    tag = None
    for ln in sublines:
        m = AVRCP.search(ln)
        if m:
            tag = ("avrcp", f"AVRCP {m.group(1).strip()}")
            break
        m = AVDTP.search(ln)
        if m:
            kind = m.group(3) or ""
            tag = ("avdtp", f"AVDTP {m.group(1)} {kind}".strip())
            break
        m = L2CAP.search(ln)
        if m:
            tag = ("l2cap", f"L2CAP {m.group(1).strip()}")
            break
    if tag is None:
        if desc.startswith(("HCI Event:", "HCI Command:")):
            name = desc.split(":", 1)[1].strip()
            name = re.sub(r"\s*\(0x[0-9a-fx|]+\)\s*plen\s+\d+$", "", name)
            if name in NOISE_EVENTS:
                return None
            kind = "event" if desc.startswith("HCI Event") else "cmd"
            tag = (kind, f"{'EVT' if kind == 'event' else 'CMD'} {name}")
        else:
            return None  # ACL audio payload etc.
    # a protocol error under the packet is the interesting part — keep it
    for ln in sublines:
        m = ERR.search(ln)
        if m:
            tag = (tag[0], f"{tag[1]} -> {m.group(1)}")
            break
    # role/mode direction is THE question for link-policy events: who
    # ended up master matters more than that a switch happened (field
    # 2026-07-27: the Skoda role-switches at connect; the crash story
    # hinges on which role the chip then streams in)
    if tag[0] == "event" and tag[1].startswith(("EVT Role Change",
                                                "EVT Mode Change")):
        for ln in sublines:
            m = re.search(r"\b(Role|Mode):\s+([A-Za-z]+)", ln)
            if m:
                tag = (tag[0], f"{tag[1]} -> {m.group(2)}")
                break
    return (direction, tag[0], tag[1])


def parse(lines):
    """btmon text -> list of (ts, direction, kind, tag), crash list."""
    packets, crashes = [], []
    cur = None  # (hdr_tuple, ts, sublines)

    def flush():
        if cur is None:
            return
        (direction, desc), ts, subs = cur
        if "Hardware Error" in desc and direction == ">":
            crashes.append(ts)
            packets.append((ts, direction, "crash", "EVT Hardware Error"))
            return
        c = classify((direction, desc), subs)
        if c:
            packets.append((ts, c[0], c[1], c[2]))

    for raw in lines:
        m = HDR.match(raw.rstrip("\n"))
        if m:
            flush()
            cur = ((m.group(1), m.group(2)), float(m.group(3)), [])
        elif cur is not None:
            cur[2].append(raw)
    flush()
    return packets, crashes


def hist(items):
    out = {}
    for it in items:
        out[it] = out.get(it, 0) + 1
    return sorted(out.items(), key=lambda kv: -kv[1])


def digest(name, lines):
    packets, crashes = parse(lines)
    if not packets:
        print(f"== {name}: no parseable btmon packets (wrong file?)")
        return
    t0, t1 = packets[0][0], packets[-1][0]
    span = max(t1 - t0, 1e-9)
    print(f"== {name}: {span:.1f}s of traffic, "
          f"{len(packets)} control packets, {len(crashes)} Hardware Error")

    avrcp = [p for p in packets if p[2] == "avrcp"]
    avdtp = [p for p in packets if p[2] == "avdtp"]
    if avrcp:
        # rate over the span AVRCP actually flowed (a capture that ran
        # for 20min before the peer connected would dilute it 10x —
        # field 2026-07-27: the boot capture's real rate was ~41/s)
        aspan = max(avrcp[-1][0] - avrcp[0][0], 1.0)
        print(f"   AVRCP: {len(avrcp)} PDUs over {aspan:.0f}s active "
              f"= {len(avrcp) / aspan:.2f}/s")
    for tag, n in hist([p[3] for p in avrcp])[:8]:
        print(f"     {n:5d}  {tag}")
    if avdtp:
        print("   AVDTP ops (stream churn — each Suspend/Start is a "
              "channel op on the live link):")
        for p in avdtp:
            print(f"     {p[0] - t0:8.1f}s  {p[3]}")
    for tag, n in hist([p[3] for p in packets
                        if p[2] in ("cmd", "l2cap", "event")])[:10]:
        print(f"     {n:5d}  {tag}")
    link = [p for p in packets if p[2] == "event"
            and any(k in p[3] for k in LINK_EVT)]
    if link:
        print("   link-policy events (sniff/role/packet-type — what a "
              "peer does to the ACL itself):")
        for p in link[:40]:
            print(f"     {p[0] - t0:8.1f}s  {p[1]} {p[3]}")
        if len(link) > 40:
            print(f"     ... and {len(link) - 40} more")

    # collapse a µs-burst of Hardware Errors into one anchor...
    anchors = []
    for ts in crashes:
        if not anchors or ts - anchors[-1][0] > 1.0:
            anchors.append([ts, 1])
        else:
            anchors[-1][1] += 1
    # ...and a reset-loop cascade into one block: once the chip wedges,
    # the kernel retries HCI Reset every ~2s, each timeout re-injects a
    # Hardware Error — dozens of anchors that all describe the SAME
    # death (field 2026-07-27: 27 of them drowned the two real crashes).
    # Only the first anchor of a tight chain gets full context.
    clusters, cur = [], None
    for a in anchors:
        if cur and a[0] - cur[-1][0] < 6.0:
            cur.append(a)
        else:
            cur = [a]
            clusters.append(cur)
    for cluster in clusters:
        ts, burst = cluster[0]
        print(f"\n   -- crash at {ts - t0:.1f}s"
              + (f" ({burst} error events in the burst)" if burst > 1
                 else ""))
        if len(cluster) > 1:
            print(f"      then a DEATH LOOP: {len(cluster) - 1} more "
                  f"Hardware Errors at ~"
                  f"{(cluster[-1][0] - ts) / (len(cluster) - 1):.1f}s "
                  f"intervals until {cluster[-1][0] - t0:.1f}s — the "
                  "kernel re-injects one per unanswered HCI Reset; the "
                  "chip's command path is dead (data may still flow)")
        before = [p for p in packets if p[0] < ts and p[2] != "crash"]
        last_churn = next((p for p in reversed(before)
                           if p[2] == "avdtp"), None)
        last_avrcp = next((p for p in reversed(before)
                           if p[2] == "avrcp"), None)
        if last_churn:
            print(f"      last AVDTP op {ts - last_churn[0]:.1f}s before "
                  f"the crash: {last_churn[3]}")
        else:
            print("      no AVDTP channel op in this capture before it")
        if last_avrcp:
            print(f"      last AVRCP PDU {ts - last_avrcp[0]:.3f}s before: "
                  f"{last_avrcp[3]}")
        last_link = next((p for p in reversed(before) if p[2] == "event"
                          and any(k in p[3] for k in LINK_EVT)), None)
        if last_link:
            print(f"      last link-policy event {ts - last_link[0]:.1f}s "
                  f"before: {last_link[3]}")
        near = [p for p in before if ts - p[0] <= NEAR_S and p[2] == "avrcp"]
        print(f"      AVRCP in the final {NEAR_S:.0f}s: {len(near)} PDUs "
              f"({len(near) / NEAR_S:.2f}/s vs {len(avrcp) / span:.2f}/s "
              "capture average)")
        print(f"      last {TAIL_N} control packets:")
        for p in before[-TAIL_N:]:
            print(f"        -{ts - p[0]:7.3f}s {p[1]} {p[3]}")


def render(path):
    """An ITERATOR of btmon text lines — never the whole text in memory.
    A 14MB snoop renders to >100MB of text; slurping that OOMed the
    512MB Zero 2 the tool is meant to run on. parse() keeps only the
    control-plane packets, which stay small."""
    if path == "-":
        return sys.stdin
    if path.endswith((".txt", ".log")):
        return open(path)
    p = subprocess.Popen(["btmon", "-r", path],
                         stdout=subprocess.PIPE, text=True)
    return p.stdout


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__.strip().split("\n\n")[0]
                 + "\n\nusage: tapbox-snoop-digest <capture.snoop|-> ...")
    for path in argv[1:]:
        digest(path, render(path))
        print()


if __name__ == "__main__":
    main(sys.argv)
