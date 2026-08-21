#!/usr/bin/env python3
"""Sonos group-awareness (owner 2026-08-21): the box shows and selects
groups as the Sonos app made them — it does not manage them.

What must hold:
1. refresh_topology: ONE call against a cached ip merges every zone
   (fresh ip/name — DHCP moves and renames self-heal), stores the group
   map with the coordinator FIRST, filters bonded invisibles, and
   REPLACES groups wholesale (a group list is only a snapshot).
2. nobody answering raises — the endpoint then serves the cache marked
   stale, so a cabin trip shows the truth, not home's ghosts.
3. players_payload: uid+name only (no LAN ips over a token-free GET),
   groups only when multi-member.
4. ui._sonos_choices: a group is one row labelled coordinator-first,
   selecting it yields the COORDINATOR uid; absorbed zones don't repeat;
   solo zones unchanged; a group naming unknown uids is skipped whole.
"""
import json
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"
sys.path.insert(0, os.path.join(REPO, "pi"))

import sonosd  # noqa: E402


def zone(uid, ip, name, visible=True):
    return types.SimpleNamespace(uid=uid, ip_address=ip,
                                 player_name=name, is_visible=visible)


STUA = zone("RINCON_A", "10.0.0.11", "Stua")
KJOKKEN = zone("RINCON_B", "10.0.0.12", "Kjøkken")
BAD = zone("RINCON_C", "10.0.0.13", "Bad")
SUB = zone("RINCON_SUB", "10.0.0.14", "Sub", visible=False)


class FakeSoCo:
    groups = []
    fail = False

    def __init__(self, ip):
        if FakeSoCo.fail:
            raise OSError("unreachable")
        self.ip = ip

    @property
    def all_groups(self):
        return FakeSoCo.groups


sonosd._soco = lambda: types.SimpleNamespace(SoCo=FakeSoCo)

# seed the cache with ONE stale record: old ip, old name
with open(sonosd.CACHE_FILE, "w") as f:
    json.dump({"players": {"RINCON_A": {"ip": "10.0.0.99",
                                        "name": "Gamlestua",
                                        "seen_at": 1.0}},
               "groups": [{"coordinator": "RINCON_A",
                           "members": ["RINCON_A", "RINCON_C"]}]}, f)

# 1. one call: zones merged with fresh ip/name, groups replaced,
#    coordinator first, the bonded sub filtered out
FakeSoCo.groups = [
    types.SimpleNamespace(coordinator=KJOKKEN,
                          members=[STUA, SUB, KJOKKEN]),
    types.SimpleNamespace(coordinator=BAD, members=[BAD]),
]
cache = sonosd.refresh_topology()
assert cache["players"]["RINCON_A"]["ip"] == "10.0.0.11", \
    "topology must heal a DHCP-moved ip"
assert cache["players"]["RINCON_A"]["name"] == "Stua", \
    "topology must heal a rename"
assert "RINCON_SUB" not in cache["players"], \
    "bonded invisibles are not pickable rooms"
assert cache["groups"] == [
    {"coordinator": "RINCON_B", "members": ["RINCON_B", "RINCON_A"]},
    {"coordinator": "RINCON_C", "members": ["RINCON_C"]},
], f"groups must replace wholesale, coordinator first: {cache['groups']}"
print("1. topology merges zones, heals ip/name, coordinator first OK")

# 2. nobody answers -> raises (endpoint serves the cache marked stale)
FakeSoCo.fail = True
try:
    sonosd.refresh_topology()
    raise AssertionError("no speaker answering must raise")
except OSError:
    pass
FakeSoCo.fail = False
print("2. unreachable household raises — cache serves marked stale OK")

# 3. the wire shape: no ips, groups only when multi-member
out = sonosd.players_payload(cache)
assert all(set(p) == {"uid", "name"} for p in out["players"]), \
    f"uid+name only over a token-free GET: {out['players']}"
assert out["groups"] == [{"coordinator": "RINCON_B",
                          "members": ["RINCON_B", "RINCON_A"]}], \
    f"solo 'groups' are noise — multi-member only: {out['groups']}"
assert "stale" not in out
assert sonosd.players_payload(cache, stale=True)["stale"] is True
print("3. payload: uid+name only, multi-member groups, stale flag OK")

# 4. the ui rows: one source for display AND selection
import ui  # noqa: E402

app = object.__new__(ui.App)
app.sonos = {"players": [{"uid": "RINCON_C", "name": "Bad"},
                         {"uid": "RINCON_B", "name": "Kjøkken"},
                         {"uid": "RINCON_A", "name": "Stua"}],
             "groups": [{"coordinator": "RINCON_B",
                         "members": ["RINCON_B", "RINCON_A"]}]}
rows = app._sonos_choices()
assert rows[0] == ("Kjøkken + Stua", "RINCON_B", ["Kjøkken", "Stua"]), \
    f"a group is one row, labelled coordinator first: {rows[0]}"
assert ("Bad", "RINCON_C", ["Bad"]) in rows, "solo zones unchanged"
assert len(rows) == 2, \
    f"absorbed members must not repeat as solo rows: {rows}"
# selecting the group row targets the coordinator — the property that
# makes everything downstream (transport verbs, ours-check) just work
assert rows[0][1] == "RINCON_B"

# a group naming a uid the player list lacks is skipped WHOLE — better
# a solo row than a group row that would mislabel what a press does
app.sonos["groups"] = [{"coordinator": "RINCON_B",
                        "members": ["RINCON_B", "RINCON_GONE"]}]
rows = app._sonos_choices()
assert len(rows) == 3 and all(len(r[2]) == 1 for r in rows), \
    f"unknown member -> no group row: {rows}"
print("4. ui rows: group=one row -> coordinator uid, no drift OK")

print("\nSONOS GROUPS OK — the box is group-aware; the Sonos app "
      "stays the group manager.")
