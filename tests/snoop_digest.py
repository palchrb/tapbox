#!/usr/bin/env python3
"""Gate the btsnoop digest parser (pi/snoopdigest.py).

The tool exists to test the 2026-07-27 hypothesis — crashes follow A2DP
Suspend/Start churn during AVRCP chatter — against real captures, so
the parser must: anchor on chip-reported Hardware Error only, collapse
µs-bursts to one anchor, attribute the last AVDTP op / AVRCP PDU with
correct deltas, keep protocol-error suffixes (Invalid Player ID), and
skip audio payload + Number-of-Completed-Packets noise. Synthetic btmon
text — the field format is pinned here so a btmon change breaks a test,
not a crash hunt."""
import io
import os
import sys
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))

import snoopdigest  # noqa: E402

SAMPLE = """\
= New Index: B8:27:EB:00:00:00 (Primary,UART,hci0)      [hci0] 0.000000
< HCI Command: Reset (0x03|0x0003) plen 0                #1 [hci0] 0.100000
> HCI Event: Command Complete (0x0e) plen 4              #2 [hci0] 0.110000
> ACL Data RX: Handle 11 flags 0x02 dlen 24              #3 [hci0] 5.000000
      Channel: 64 len 20 [PSM 23 mode Basic (0x00) {chan 0}]
      AVCTP Control: Command: type 0x00 label 1 PID 0x110e
        AVRCP: GetPlayStatus pt Single len 0x0000
> ACL Data RX: Handle 11 flags 0x02 dlen 24              #4 [hci0] 6.000000
      Channel: 64 len 20 [PSM 23 mode Basic (0x00) {chan 0}]
      AVCTP Control: Command: type 0x00 label 2 PID 0x110e
        AVRCP: RegisterNotification pt Single len 0x0005
< ACL Data TX: Handle 11 flags 0x00 dlen 18              #5 [hci0] 6.010000
      Channel: 64 len 14 [PSM 23 mode Basic (0x00) {chan 0}]
      AVCTP Control: Response: type 0x00 label 2 PID 0x110e
        AVRCP: RegisterNotification pt Single len 0x0001
          Error: 0x11 (Invalid Player ID)
> ACL Data RX: Handle 11 flags 0x02 dlen 400             #6 [hci0] 6.500000
      Channel: 66 len 396 [PSM 25 mode Basic (0x00) {chan 1}]
> HCI Event: Number of Completed Packets (0x13) plen 5   #7 [hci0] 6.600000
< ACL Data TX: Handle 11 flags 0x00 dlen 12              #8 [hci0] 8.000000
      Channel: 65 len 8 [PSM 25 mode Basic (0x00) {chan 1}]
      AVDTP: Suspend (0x09) Command (0x00) type 0x00 label 5 nosp 0
> ACL Data RX: Handle 11 flags 0x02 dlen 12              #9 [hci0] 8.050000
      Channel: 65 len 8 [PSM 25 mode Basic (0x00) {chan 1}]
      AVDTP: Suspend (0x09) Response Accept (0x02) type 0x00 label 5
< ACL Data TX: Handle 11 flags 0x00 dlen 12              #10 [hci0] 11.000000
      Channel: 65 len 8 [PSM 25 mode Basic (0x00) {chan 1}]
      AVDTP: Start (0x07) Command (0x00) type 0x00 label 6 nosp 0
> HCI Event: Mode Change (0x14) plen 6                   #21 [hci0] 15.000000
        Status: Success (0x00)
        Handle: 11
        Mode: Sniff (0x02)
        Interval: 800.000 msec (0x0500)
> ACL Data RX: Handle 11 flags 0x02 dlen 24              #11 [hci0] 19.000000
      Channel: 64 len 20 [PSM 23 mode Basic (0x00) {chan 0}]
      AVCTP Control: Command: type 0x00 label 3 PID 0x110e
        AVRCP: GetPlayStatus pt Single len 0x0000
> HCI Event: Hardware Error (0x10) plen 1                #12 [hci0] 23.000000
> HCI Event: Hardware Error (0x10) plen 1                #13 [hci0] 23.000040
> HCI Event: Hardware Error (0x10) plen 1                #14 [hci0] 23.000090
< HCI Command: Reset (0x03|0x0003) plen 0                #15 [hci0] 25.000000
> HCI Event: Hardware Error (0x10) plen 1                #16 [hci0] 25.500000
< HCI Command: Reset (0x03|0x0003) plen 0                #17 [hci0] 27.000000
> HCI Event: Hardware Error (0x10) plen 1                #18 [hci0] 27.500000
< HCI Command: Reset (0x03|0x0003) plen 0                #19 [hci0] 29.000000
> HCI Event: Hardware Error (0x10) plen 1                #20 [hci0] 29.500000
"""

buf = io.StringIO()
with redirect_stdout(buf):
    snoopdigest.digest("sample", SAMPLE.splitlines(keepends=True))
out = buf.getvalue()
print(out)

# 1. the µs burst collapses to one anchor (size kept), and the kernel's
#    reset-loop cascade collapses to ONE context block — 27 loop anchors
#    drowned the real crashes in the 2026-07-27 wedge capture
assert "6 Hardware Error" in out.split("\n")[0], out.split("\n")[0]
assert "3 error events in the burst" in out
assert out.count("-- crash at") == 1, "cascade must not get own blocks"
assert "DEATH LOOP: 3 more" in out
print("1. burst + reset-loop cascade collapsed to one anchor OK")

# 2. AVDTP churn attributed: last op is the Start at 11.0s, crash 23.0s
assert "last AVDTP op 12.0s before the crash: AVDTP Start Command" in out
print("2. Suspend/Start churn delta computed OK")

# 3. last AVRCP PDU attribution with the sub-second delta
assert "last AVRCP PDU 4.000s before: AVRCP GetPlayStatus" in out
print("3. last AVRCP PDU attributed OK")

# 4. the protocol-error suffix survives classification
assert "AVRCP RegisterNotification -> Invalid Player ID" in out
print("4. Invalid Player ID suffix kept OK")

# 5. audio payload and NOCP bookkeeping are NOT in the control tail
assert "Number of Completed Packets" not in out
tail = out[out.index("last 30 control packets"):]
assert "PSM 25" not in tail and "dlen 400" not in tail
print("5. audio + NOCP noise skipped OK")

# 6. histograms present: AVRCP mix and the ACTIVE-span rate (a capture
#    idling long before the peer connects must not dilute the rate)
assert "s active = " in out and "AVRCP GetPlayStatus" in out
assert "CMD Reset" in out
print("6. capture-wide histograms + active-span rate OK")

# 7. link-policy events (sniff/role/packet-type) get their own timeline
#    AND a per-crash delta — the prime suspect once the pre-mpris
#    capture crashed at a calm 2 PDU/s (2026-07-27)
assert "link-policy events" in out
assert "EVT Mode Change -> Sniff" in out, "direction must be extracted"
assert "last link-policy event 8.0s before: EVT Mode Change -> Sniff" in out
print("7. link-policy timeline + direction + per-crash delta OK")

# 8. the media envelope: outbound (host->chip) unclassified ACL is the
#    audio payload — its flow segments and the silent tail are what
#    exposed the 2026-07-30 freeze (1MB capture in a 15MB-per-window
#    session; the stream died ~3min before the kernel logged anything).
#    Inbound unclassified ACL must NOT count, and the payload still
#    stays out of the control tail.
FREEZE = """\
< ACL Data TX: Handle 11 flags 0x00 dlen 400             #1 [hci0] 10.000000
      Channel: 66 len 396 [PSM 25 mode Basic (0x00) {chan 1}]
> HCI Event: Number of Completed Packets (0x13) plen 5   #2 [hci0] 10.100000
< ACL Data TX: Handle 11 flags 0x00 dlen 400             #3 [hci0] 10.500000
      Channel: 66 len 396 [PSM 25 mode Basic (0x00) {chan 1}]
> HCI Event: Number of Completed Pack.. (0x13) plen 5  #10001 [hci0] 10.600000
< ACL Data TX: Handle 11 flags 0x00 dlen 400          #10002 [hci0] 30.000000
      Channel: 66 len 396 [PSM 25 mode Basic (0x00) {chan 1}]
< ACL Data TX: Handle 11 flags 0x00 dlen 400          #10003 [hci0] 31.000000
      Channel: 66 len 396 [PSM 25 mode Basic (0x00) {chan 1}]
> ACL Data RX: Handle 11 flags 0x02 dlen 400          #10004 [hci0] 200.000000
      Channel: 67 len 396 [PSM 25 mode Basic (0x00) {chan 2}]
< HCI Command: Disconnect (0x01|0x0006) plen 3        #10005 [hci0] 300.000000
"""
buf = io.StringIO()
with redirect_stdout(buf):
    snoopdigest.digest("freeze", FREEZE.splitlines(keepends=True))
out = buf.getvalue()
assert "outbound ACL flow" in out, out
flat = " ".join(out.split())
assert "0.0s -> 0.5s 2 frames" in flat, out
assert "20.0s -> 21.0s 2 frames" in flat, out
assert sum(1 for ln in out.splitlines()
           if ln.strip().endswith("frames")) == 2, \
    "the 20s gap must split the segments"
assert "SILENT for the final 269.0s" in out, out
assert "290.0s of traffic" in out.split("\n")[0], \
    "the span must cover media, not just control packets"
# completion tracking: both the full and the '..'-elided form count
# (btmon elides long names once the packet-number column widens — 15817
# leaked past the exact-match noise filter in the 2026-07-30 capture),
# they stay out of the control histogram, and the unacked tail is named
assert "completions (NOCP): 2 acks, last at 0.6s" in out, out
assert "2 outbound frames after the last ack" in out, out
assert "Completed Pack.." not in out, "elided NOCP must not leak"
assert "1 control packets" in out.split("\n")[0], out.split("\n")[0]
print("8. media envelope: segments, gap split, silent tail, NOCP OK")

print("all snoop_digest checks passed")
