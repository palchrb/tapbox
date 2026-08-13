#!/usr/bin/env python3
"""Gate play.sh against the API token.

`sudo ./play.sh "<spotify url>"` is a documented entry point (it is in
install.sh's own header), and it posts a RAW target — which became
privileged when the API gate landed, because a raw target can put
uncurated audio in a kid's room. Without the header that documented
command would just 401.

play.sh uses curl, not boxapi, so it can't inherit the header the way
ui.py and btwatchd do — it reads the token file itself. This test runs
the real script against a fake daemon and checks what actually arrives
on the wire."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAY = os.path.join(REPO, "pi", "play.sh")
TMP = tempfile.mkdtemp()
TOKEN = "K7M2P9QR4TVX8N3Z"
TOKEN_FILE = os.path.join(TMP, "api-token")
with open(TOKEN_FILE, "w") as f:
    f.write(TOKEN + "\n")  # trailing newline, as the real file has

SEEN = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        SEEN.append({"path": self.path,
                     "token": self.headers.get("X-Vibb-Token"),
                     "ctype": self.headers.get("Content-Type"),
                     "body": json.loads(body or b"{}")})
        out = b'{"ok": true, "source": "spotify"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        out = b'{"volume": 50, "playing": false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def run(args, token_file=TOKEN_FILE):
    env = dict(os.environ, VIBB_DAEMON=f"http://127.0.0.1:{port}",
               VIBB_TOKEN_FILE=token_file)
    return subprocess.run(["bash", PLAY, *args], env=env,
                          capture_output=True, text=True, timeout=60)


# play.sh hardcodes the daemon URL, so point it at the fake one by
# rewriting just that line into a temp copy (the script is the artifact
# under test — everything else is verbatim).
src = open(PLAY).read().replace('DAEMON="http://127.0.0.1:3679"',
                                f'DAEMON="http://127.0.0.1:{port}"')
# the go-librespot reachability probe points at the same fake
src = src.replace('API="http://127.0.0.1:3678"',
                  f'API="http://127.0.0.1:{port}"')
# the script insists on root (it pokes bluez); the token/curl path under
# test doesn't need it, so drop just that guard in the copy
src = src.replace("if [[ $EUID -ne 0 ]]; then", "if false; then")
# ...and keep it pointed at the repo's bt.py (the copy lives in /tmp, so
# its relative lookup would miss and fall back to the installed path)
# ...and stub the bluetooth helper: this test is about what goes on the
# wire to the daemon, not about bringing a real radio up
src = src.replace('bt_py() { python3 "$BT_PY" "$@"; }',
                  'bt_py() { return 0; }')
PLAY = os.path.join(TMP, "play.sh")
with open(PLAY, "w") as f:
    f.write(src)

# 1. THE DOCUMENTED COMMAND: playing a raw link must carry the token,
#    or `sudo ./play.sh "<url>"` is simply broken after the gate
SEEN.clear()
r = run(["https://open.spotify.com/track/abc123"])
assert SEEN, f"play.sh made no request: {r.stdout}\n{r.stderr}"
call = SEEN[-1]
assert call["path"] == "/play", call
assert call["token"] == TOKEN, \
    f"play.sh must send the box token for a raw target: {call}"
assert call["body"].get("target"), call
print("1. play.sh sends the token when playing a raw link OK")

# 2. the Content-Type guard is still satisfied (the CSRF fix)
assert call["ctype"].startswith("application/json"), call
print("2. play.sh still sends application/json OK")

# 3. transport verbs go through the same path
SEEN.clear()
run(["pause"])
assert SEEN and SEEN[-1]["token"] == TOKEN, SEEN
print("3. transport verbs carry the token too OK")

# 4. NO token file (a box where /etc/vibb is unreadable, or a non-root
#    caller): the script must still RUN — it just gets a clean 401 from
#    the daemon rather than dying on a missing file
SEEN.clear()
r = run(["https://open.spotify.com/track/abc123"],
        token_file=os.path.join(TMP, "nonexistent"))
assert r.returncode == 0, f"play.sh must not crash without a token: {r.stderr}"
assert SEEN and SEEN[-1]["token"] is None, SEEN
print("4. missing token file: still runs, just unauthenticated OK")

srv.shutdown()
print("\nall play_sh_token checks passed")
