#!/usr/bin/env python3
"""Gate the API token primitive (SECURITY.md Model B).

Every rule here is FAIL-CLOSED. The one that matters most:
`hmac.compare_digest("", "")` returns True, so a truncated or empty
token file would authorize every client sending an empty header — the
box would look locked and be wide open. verify() must refuse empty on
both sides (QA review 2026-07-25, blocking item 4).

Also pinned: ensure() never rewrites a valid token (a transient error
that rotated the secret would silently unlink every phone in the house),
rotation is visible without a restart (the screen can re-link a phone
immediately), and the file is 0600 from creation."""
import os
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "etc", "api-token")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import token  # noqa: E402

F = token.TOKEN_FILE

# 1. ensure() creates a token; 0600 from creation, and the value is
#    Crockford base32 of the documented length
t = token.ensure()
assert len(t) == token.LENGTH, t
assert all(c in token.ALPHABET for c in t), t
mode = stat.S_IMODE(os.stat(F).st_mode)
assert mode == 0o600, f"token file must be 0600, got {oct(mode)}"
print("1. ensure() creates a 0600 Crockford-base32 token OK")

# 2. ensure() is idempotent — it must NEVER rotate an existing valid
#    token (that would unlink every linked phone behind the user's back)
again = token.ensure()
assert again == t, "ensure() must not rewrite a valid token"
print("2. ensure() is idempotent (linked phones survive) OK")

# 3. verify(): the real token passes in any human-typed form
assert token.verify(t)
assert token.verify(t.lower()), "typing must be case-insensitive"
assert token.verify(token.grouped(t)), "the dashed screen form must work"
assert token.verify("  " + token.grouped(t).replace("-", " ") + "  ")
print("3. verify() accepts lowercase / dashed / spaced forms OK")

# 3b. Crockford aliases: the characters a parent misreads off a 240px
#     screen (I/L for 1, O for 0) fold to the real ones
assert token.normalize("O0IL1") == "00111"
assert token.normalize("u") == "V"
print("3b. Crockford I/L->1, O->0, U->V aliases fold OK")

# 4. a wrong token is refused (one character off)
other = ("1" if t[0] != "1" else "2") + t[1:]
assert not token.verify(other)
print("4. a one-character-off token is refused OK")

# 5. THE BIG ONE: empty candidate must NEVER pass — compare_digest("","")
#    is True, so this is the difference between locked and wide open
assert not token.verify("")
assert not token.verify(None)
assert not token.verify("----")  # normalizes to ""
print("5. empty/blank candidates are refused (compare_digest trap) OK")

# 6. a TRUNCATED/corrupt token file denies everything — it must not
#    become a short, guessable, or empty accepted secret
with open(F, "w") as f:
    f.write("ABC\n")
assert token.read() == "", "a too-short token file must read as unusable"
assert not token.verify("ABC"), "a corrupt token must not authorize anyone"
assert not token.verify("")
print("6. truncated token file: denies everything (fail closed) OK")

# 6b. ...and ensure() then HEALS it (there is no usable token, so
#     creating one is correct here — unlike case 2)
healed = token.ensure()
assert len(healed) == token.LENGTH and healed != t
print("6b. ensure() heals a corrupt token file OK")

# 7. a missing file denies, without raising
os.remove(F)
assert token.read() == ""
assert not token.verify("ANYTHING")
assert token.header() == {}, "header() must be best-effort, never raise"
print("7. missing token file: denies, header() empty, no exception OK")

# 8. rotation is visible WITHOUT a restart (the screen re-links a phone
#    immediately) — the mtime+inode cache must notice the new file
first = token.ensure()
assert token.verify(first)
second = token.rotate()
assert second != first
assert token.verify(second), "the new token must work at once"
assert not token.verify(first), "the old token must stop working at once"
print("8. rotate(): new token live immediately, old one dead OK")

# 9. header() carries the token for internal callers
h = token.header()
assert h == {"X-Vibb-Token": second}, h
print("9. header() carries the token for internal callers OK")

# 10. grouped() is the screen form
g = token.grouped(second)
assert g.count("-") == 3 and g.replace("-", "") == second, g
print("10. grouped() renders XXXX-XXXX-XXXX-XXXX OK")

print("\nall api_token checks passed")
