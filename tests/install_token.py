#!/usr/bin/env python3
"""Gate install.sh's half of the API token.

The property that matters most is IDEMPOTENCE: install.sh doubles as the
updater, so people run it after every git pull. If it rotated the token,
every phone in the house would silently stop working after a routine
update — and the only clue would be a 401 banner.

Also pinned: the token exists BEFORE the services restart (so the daemon
never starts without one), and the QR lib is in the venv IMPORT PROBE and
not only the pip line — a box that already has the other libs skips the
install entirely, and the QR would never ship."""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(REPO, "pi", "install.sh")
src = open(SH).read()

# 1. the generator install.sh calls is token.ensure() — the same function
#    the daemon self-heals with, so there is exactly one implementation
assert "from vibb import token; token.ensure()" in src, \
    "install.sh must generate via token.ensure(), not its own generator"
print("1. install.sh generates through token.ensure() OK")

# 2. IDEMPOTENCE, for real: run the generator twice against a temp file
TMP = tempfile.mkdtemp()
env = dict(os.environ, VIBB_TOKEN_FILE=os.path.join(TMP, "api-token"),
           PYTHONPATH=os.path.join(REPO, "pi"))
gen = "import sys; from vibb import token; print(token.ensure())"
first = subprocess.run([sys.executable, "-c", gen], env=env,
                       capture_output=True, text=True, check=True).stdout.strip()
second = subprocess.run([sys.executable, "-c", gen], env=env,
                        capture_output=True, text=True, check=True).stdout.strip()
assert first and first == second, \
    f"re-running install.sh must NOT rotate the token ({first} -> {second})"
print("2. re-running install.sh keeps the token (linked phones survive) OK")

# 3. the token is created BEFORE the service restarts, so the daemon
#    never comes up without one
i_token = src.index("from vibb import token; token.ensure()")
i_restart = src.index('echo "==> [6/8] Enabling services')
assert i_token < i_restart, \
    "the token must be generated before the services are restarted"
print("3. token generated before the service restart block OK")

# 4. qrcode is in the venv IMPORT PROBE, not just the pip line. Without
#    this an existing box passes the probe, skips the install, and the
#    'Link phone' screen silently degrades to text forever.
probe = re.search(r"python3 -c 'import ([^']+)'", src)
assert probe and "qrcode" in probe.group(1), \
    f"qrcode missing from the venv import probe: {probe and probe.group(1)}"
assert "gpiozero qrcode" in src or "qrcode" in src.split("pip install")[1][:200], \
    "qrcode missing from the pip install line"
print("4. qrcode is in both the import probe and the pip line OK")

# 5. install.sh must NOT print the secret — it should only appear when
#    someone explicitly asks. An install log (or a scrollback pasted
#    while debugging) must not carry it.
i_fn = src.index("print_token() {")
fn = src[i_fn:src.index("}", i_fn)]
assert "API_TOKEN" not in fn and "api-token" not in fn, \
    f"install.sh must not print the token itself:\n{fn}"
assert "vibb-token" in fn, "it must point at the vibb-token command"
assert src.count("print_token") >= 3, \
    "print_token must be called on the exit paths, not just defined"
print("5. install.sh points at vibb-token, never prints the secret OK")

# 5b. ...and the command that DOES print it is installed
assert "/usr/local/bin/vibb-token" in src, \
    "install.sh must install the vibb-token command"
tok_sh = open(os.path.join(REPO, "pi", "token.sh")).read()
assert "token.ensure()" in tok_sh and "token.rotate()" in tok_sh, \
    "vibb-token must use the shared token module, not its own generator"
assert ".local:3679/#t=" in tok_sh, \
    "the pairing link must use the stable .local origin"
print("5b. vibb-token is installed and reuses the shared module OK")

# 6. it must not be world-readable anywhere it lands
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
import stat  # noqa: E402
assert stat.S_IMODE(os.stat(env["VIBB_TOKEN_FILE"]).st_mode) == 0o600
print("6. the generated token file is 0600 OK")

print("\nall install_token checks passed")
