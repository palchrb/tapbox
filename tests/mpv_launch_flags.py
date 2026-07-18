#!/usr/bin/env python3
"""Pin mpv's launch argv. The startup 'trims' (--ao=alsa, --no-config,
--load-scripts=no, --no-ytdl) are safe to ADD for a faster cold start, but
the audio-critical flags must NEVER be dropped by a future trim:
--audio-samplerate=44100 / --audio-channels=stereo force the 44.1kHz stereo
resample without which low-bitrate audiobooks play SILENTLY over A2DP
(player.py's own comment records the field bug). This gate makes that
regression impossible to land silently."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

cmd = player.mpv_command(["/cache/show/e1.mp3", "https://x/e2.mp3"],
                         40, "/run/tapbox-mpv.sock", "tapbox_bt")

# 1. the two flags that keep audiobooks audible over BT are present
assert "--audio-samplerate=44100" in cmd, "44.1kHz resample flag missing"
assert "--audio-channels=stereo" in cmd, "stereo resample flag missing"
print("1. the 44.1kHz/stereo resample flags are present OK")

# 2. output routing + volume + ipc + the queue all make it through
assert "--audio-device=alsa/tapbox_bt" in cmd, "output pcm not routed"
assert "--volume=40" in cmd, "box volume not applied"
assert "--input-ipc-server=/run/tapbox-mpv.sock" in cmd, "ipc socket missing"
assert cmd[-2:] == ["/cache/show/e1.mp3", "https://x/e2.mp3"], "queue lost"
print("2. output pcm, volume, ipc socket and the queue are all passed OK")

# 3. the startup trims are present (fast cold start) and harmless
for f in ("--ao=alsa", "--no-config", "--load-scripts=no", "--no-ytdl"):
    assert f in cmd, f"startup trim {f} missing"
assert "--no-video" in cmd and "--really-quiet" in cmd
print("3. startup trims present (ao=alsa, no-config, no scripts, no ytdl) OK")

print("MPV LAUNCH FLAGS OK — audio-critical flags pinned; trims are additive.")
