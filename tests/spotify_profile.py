#!/usr/bin/env python3
"""Gate profile-follow sections: a section with a spotify_user mirrors that
profile's PUBLIC playlists via the client-credentials Web API — parent
curates by flipping playlists public/private on their phone. Manual entries
always win (no duplicate ids), and a failed fetch must never wipe the
entries from the last good sweep."""
import json
import os
import sys
import tempfile
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "library.json")
CREDS = os.path.join(STATE, "spotify-api.json")
os.environ["TAPBOX_SPOTIFY_API"] = CREDS
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import library, spotify_web  # noqa: E402

# 1. parse_user: share links, URIs and plain names all land on the username
assert spotify_web.parse_user("palchrb") == "palchrb"
assert spotify_web.parse_user(
    "https://open.spotify.com/user/palchrb?si=abc123") == "palchrb"
assert spotify_web.parse_user("spotify:user:palchrb") == "palchrb"
assert spotify_web.parse_user("  ") is None
print("1. parse_user handles links, URIs and plain usernames OK")

# --- fake the network: token endpoint + two pages of playlists -----------------
with open(CREDS, "w") as f:
    json.dump({"client_id": "id", "client_secret": "sec"}, f)

TOKENS = []
P = "https://open.spotify.com/playlist/"


def _pl(pid, name):
    return {"id": pid, "name": name,
            "images": [{"url": f"http://img/{pid}-640", "width": 640},
                       {"url": f"http://img/{pid}-300", "width": 300}]}


def fake_http(req, timeout=10):
    url = req.full_url
    if url == spotify_web.ACCOUNTS:
        TOKENS.append(1)
        return {"access_token": "tok", "expires_in": 3600}
    if "/users/ghost/" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
    if "/users/kidsdad/" in url and "offset=50" not in url:
        return {"items": [_pl("aaa", "Barnesanger"), None,
                          _pl("bbb", "Rolig kveld")],
                "next": spotify_web.API +
                        "/users/kidsdad/playlists?offset=50&limit=50"}
    if "offset=50" in url:
        return {"items": [_pl("ccc", "Lørdagsdisco")], "next": None}
    raise AssertionError(f"unexpected URL {url}")


spotify_web._http = fake_http

# 2. user_playlists: pages followed, deleted (null) rows skipped, the
# ~300px cover picked, and the app token fetched exactly once
pls = spotify_web.user_playlists("kidsdad")
assert [p["name"] for p in pls] == ["Barnesanger", "Rolig kveld",
                                    "Lørdagsdisco"]
assert pls[0]["target"] == P + "aaa"
assert pls[0]["image"] == "http://img/aaa-300", pls[0]["image"]
assert len(TOKENS) == 1, "token not cached across pages"
spotify_web.user_playlists("kidsdad")
assert len(TOKENS) == 1, "token re-fetched while still valid"
print("2. playlists paged, nulls skipped, token cached OK")

# 3. normalize_library: spotify_user accepted (stripped), junk rejected
lib = library.normalize_library(
    {"sections": [{"name": "Pappas", "spotify_user": " kidsdad ",
                   "entries": []}]})
assert lib["sections"][0]["spotify_user"] == "kidsdad"
try:
    library.normalize_library(
        {"sections": [{"name": "X", "spotify_user": ["nope"],
                       "entries": []}]})
    raise AssertionError("non-string spotify_user accepted")
except ValueError:
    pass
print("3. spotify_user validated on sections OK")

# 4. sync: the follow section fills with the profile's playlists, but a
# playlist already curated manually keeps its manual home (no dup ids)
library.save_library(library.normalize_library({"sections": [
    {"name": "Musikk", "entries": [
        {"name": "Min versjon", "target": P + "bbb"}]},
    {"name": "Pappas", "spotify_user": "kidsdad", "entries": []},
]}))
assert library.sync_profile_sections() is True
lib = library.load_library()
pappas = lib["sections"][1]
assert [e["name"] for e in pappas["entries"]] == \
    ["Barnesanger", "Lørdagsdisco"], pappas["entries"]
assert all(e["id"] for e in pappas["entries"])
assert lib["sections"][0]["entries"][0]["name"] == "Min versjon"
print("4. follow section filled, manual duplicate left alone OK")

# 5. no change -> no save (the sweeper must not rewrite the file each pass)
saves = []
real_save = library.save_library
library.save_library = lambda lib: saves.append(1) or real_save(lib)
assert library.sync_profile_sections() is False
assert not saves, "library rewritten although nothing changed"
library.save_library = real_save
print("5. unchanged profile is a no-op sweep OK")

# 6. a failing profile keeps last sweep's entries (never wipe on a blip)
lib = library.load_library()
lib["sections"][1]["spotify_user"] = "ghost"
library.save_library(lib)
assert library.sync_profile_sections() is False
kept = library.load_library()["sections"][1]["entries"]
assert [e["name"] for e in kept] == ["Barnesanger", "Lørdagsdisco"]
print("6. fetch failure keeps the previous entries OK")

# 7. two sections following the same profile: second gets no duplicates
library.save_library(library.normalize_library({"sections": [
    {"name": "Pappas", "spotify_user": "kidsdad", "entries": []},
    {"name": "Pappas igjen", "spotify_user": "kidsdad", "entries": []},
]}))
assert library.sync_profile_sections() is True
lib = library.load_library()
assert len(lib["sections"][0]["entries"]) == 3
assert lib["sections"][1]["entries"] == []
print("7. duplicate follows never create duplicate entries OK")

print("SPOTIFY PROFILE OK — public playlists follow the profile, "
      "manual curation wins.")
