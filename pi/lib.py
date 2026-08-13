#!/usr/bin/env python3
"""vibb-lib — manage the parent-curated library from the terminal.

The library (/etc/vibb/library.json) is the menu the screen UI and the
parent PWA render: sections of named links. This CLI is the pre-PWA way
to fill it. The daemon (vibbd) serves it via GET/PUT /library and
expands entries via GET /expand.

Usage:
  sudo vibb-lib list
  sudo vibb-lib add <section> <name> <target> [auto|newest|oldest]
  sudo vibb-lib rm <entry-id>
  sudo vibb-lib order <entry-id> auto|newest|oldest
  sudo vibb-lib cache <entry-id> <n>     keep the newest n episodes offline
                                           (0 = no offline copies, default)

Examples:
  sudo vibb-lib add Stories Fantorangen \\
      https://radio.nrk.no/podkast/fantorangenfortellinger
  sudo vibb-lib add Music "Kids songs" \\
      "https://open.spotify.com/playlist/..."
  sudo vibb-lib add Audiobooks "Book X" /var/lib/vibb/local/book-x oldest

'order' controls playback/menu direction for multi-episode links:
auto = the service's natural order (NRK podcasts newest first, series and
folders oldest first, RSS as the feed lists them).
"""

import hashlib
import json
import os
import re
import sys

LIB_FILE = os.environ.get("VIBB_LIBRARY", "/etc/vibb/library.json")
ORDERS = {"auto": "auto", "newest": "newest_first", "oldest": "oldest_first",
          "newest_first": "newest_first", "oldest_first": "oldest_first"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def load():
    try:
        with open(LIB_FILE) as f:
            lib = json.load(f)
        assert isinstance(lib.get("sections"), list)
        return lib
    except (OSError, ValueError, AssertionError):
        return {"version": 1, "sections": []}


def save(lib):
    os.makedirs(os.path.dirname(LIB_FILE), exist_ok=True)
    tmp = LIB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIB_FILE)


def entries(lib):
    for s in lib["sections"]:
        for e in s["entries"]:
            yield s, e


def cmd_list(lib, _args):
    if not lib["sections"]:
        print("library is empty — add something:\n"
              "  sudo vibb-lib add <section> <name> <link>")
        return
    for s in lib["sections"]:
        print(f"{s['name']}")
        for e in s["entries"]:
            order = "" if e.get("order", "auto") == "auto" else f"  [{e['order']}]"
            print(f"  {e['id']}  {e['name']}{order}")
            print(f"            {e['target']}")


def cmd_add(lib, args):
    if len(args) < 3:
        sys.exit("usage: vibb-lib add <section> <name> <target> [auto|newest|oldest]")
    section_name, name, target = args[0], args[1], args[2]
    order = ORDERS.get(args[3] if len(args) > 3 else "auto")
    if not order:
        sys.exit(f"order must be one of: {', '.join(sorted(set(ORDERS)))}")
    sec = next((s for s in lib["sections"]
                if s["name"].lower() == section_name.lower()), None)
    if sec is None:
        sec = {"id": slug(section_name), "name": section_name, "entries": []}
        lib["sections"].append(sec)
    eid = hashlib.sha1(target.encode()).hexdigest()[:8]
    if any(e["id"] == eid for _s, e in entries(lib)):
        sys.exit(f"already in the library (entry {eid})")
    sec["entries"].append({"id": eid, "name": name, "target": target,
                           "order": order})
    save(lib)
    print(f"added {eid}: {section_name} / {name}")


def cmd_rm(lib, args):
    if len(args) != 1:
        sys.exit("usage: vibb-lib rm <entry-id>")
    for s in lib["sections"]:
        for e in s["entries"]:
            if e["id"] == args[0]:
                s["entries"].remove(e)
                lib["sections"] = [x for x in lib["sections"] if x["entries"]]
                save(lib)
                print(f"removed {e['name']}")
                return
    sys.exit(f"no entry {args[0]} (see: vibb-lib list)")


def cmd_order(lib, args):
    if len(args) != 2 or args[1] not in ORDERS:
        sys.exit("usage: vibb-lib order <entry-id> auto|newest|oldest")
    for _s, e in entries(lib):
        if e["id"] == args[0]:
            e["order"] = ORDERS[args[1]]
            save(lib)
            print(f"{e['name']}: order = {e['order']}")
            return
    sys.exit(f"no entry {args[0]} (see: vibb-lib list)")


def cmd_cache(lib, args):
    if len(args) != 2 or not args[1].isdigit() or not 0 <= int(args[1]) <= 100:
        sys.exit("usage: vibb-lib cache <entry-id> <0-100>")
    for _s, e in entries(lib):
        if e["id"] == args[0]:
            e["cache"] = int(args[1])
            save(lib)
            print(f"{e['name']}: keeps the newest {e['cache']} offline"
                  if e["cache"] else f"{e['name']}: no offline copies")
            return
    sys.exit(f"no entry {args[0]} (see: vibb-lib list)")


def main():
    cmds = {"list": cmd_list, "add": cmd_add, "rm": cmd_rm,
            "order": cmd_order, "cache": cmd_cache}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)
    cmds[sys.argv[1]](load(), sys.argv[2:])


if __name__ == "__main__":
    main()
