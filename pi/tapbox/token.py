"""The box's API token — the credential that gates privileged endpoints.

The trust anchor is PHYSICAL POSSESSION: the token is shown on the box's
own screen (QR + typeable text, Settings -> Link phone), so linking a
phone proves you were standing at the box. See SECURITY.md.

Everything here is FAIL-CLOSED. A missing, unreadable, empty or
suspiciously short token file must deny access, never open it — a
half-written file must not become an open box. `verify()` is the only
comparison callers should use; it refuses empty candidates explicitly,
because `hmac.compare_digest("", "")` returns True and a truncated token
file would otherwise authorize every client that sends an empty header.
"""

import hmac
import os
import secrets

# Crockford base32: no I/L/O/U, so nothing a parent can misread off a
# 240px screen (1/I/l and 0/O are the classic confusions) and no vowels
# to accidentally spell words. Uppercase only -> case-insensitive typing.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 16  # 16 * 5 bits = 80 bits of entropy
MIN_LENGTH = 8  # anything shorter is treated as corrupt, never accepted

TOKEN_FILE = os.environ.get("TAPBOX_TOKEN_FILE", "/etc/tapbox/api-token")

# (st_mtime_ns, st_ino) -> value. Re-stat'ed on every read so a rotation
# from the screen takes effect on the NEXT request with no restart; the
# inode is part of the key because rotate() writes atomically via rename,
# which can reuse an mtime but never the inode.
_CACHE = {"key": None, "value": ""}

# Crockford's canonical decode aliases: the characters a human is most
# likely to type instead of the real ones.
_ALIASES = {"I": "1", "L": "1", "O": "0", "U": "V"}


def normalize(raw):
    """Fold a human-typed token to canonical form: uppercase, no
    separators, and Crockford's I/L->1, O->0, U->V aliases. So
    'xxxx-xxxx' pasted with dashes, spaces or lowercase still matches."""
    if not raw:
        return ""
    out = []
    for ch in str(raw).upper():
        ch = _ALIASES.get(ch, ch)
        if ch in ALPHABET:
            out.append(ch)
    return "".join(out)


def generate():
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def read():
    """The current token, or "" when there is none/it is unusable.
    Callers must treat "" as 'deny everything privileged'."""
    try:
        st = os.stat(TOKEN_FILE)
    except OSError:
        _CACHE["key"], _CACHE["value"] = None, ""
        return ""
    key = (st.st_mtime_ns, st.st_ino)
    if _CACHE["key"] == key:
        return _CACHE["value"]
    try:
        with open(TOKEN_FILE) as f:
            value = normalize(f.read())
    except OSError:
        value = ""
    if len(value) < MIN_LENGTH:
        value = ""  # empty/truncated/corrupt -> fail closed
    _CACHE["key"], _CACHE["value"] = key, value
    return value


def verify(candidate):
    """Constant-time check of a client-supplied token. False whenever
    either side is empty — see the module docstring."""
    want = read()
    got = normalize(candidate)
    if not want or not got:
        return False
    return hmac.compare_digest(want, got)


def _write(value):
    """0600 from creation (never chmod after — that leaves a window),
    then atomic rename so a reader never sees a partial file."""
    d = os.path.dirname(TOKEN_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, TOKEN_FILE)
    _CACHE["key"] = None  # force a re-read
    return value


def ensure():
    """Return the existing token, creating one only if there ISN'T a
    usable one. Never rewrites a valid token: a transient read error that
    silently rotated the secret would unlink every phone in the house."""
    current = read()
    if current:
        return current
    return _write(generate())


def rotate():
    """New token; every linked phone must re-link. Called directly by the
    on-box UI (which is trusted by physical possession) — deliberately
    NOT exposed as an HTTP endpoint, so no request can ever rotate or
    read the secret."""
    return _write(generate())


def grouped(value=None):
    """'XXXX-XXXX-XXXX-XXXX' — the form shown on the screen for typing."""
    v = value or read()
    return "-".join(v[i:i + 4] for i in range(0, len(v), 4))


def header():
    """Auth header for internal callers, best-effort: an unreadable token
    yields {} rather than an exception, so the thin daemons that only use
    SAFE endpoints keep working even without read access."""
    v = read()
    return {"X-TapBox-Token": v} if v else {}
