#!/usr/bin/env python3
"""tapbox-lib — manage the parent-curated library from the terminal.

The library (/etc/tapbox/library.json) is the menu the screen UI and the
parent PWA render: sections of named links. This CLI is the pre-PWA way
to fill it. The daemon (tapboxd) serves it via GET/PUT /library and
expands entries via GET /expand.

Usage:
  sudo tapbox-lib list
  sudo tapbox-lib add <section> <name> <target> [auto|newest|oldest]
  sudo tapbox-lib rm <entry-id>
  sudo tapbox-lib order <entry-id> auto|newest|oldest

Examples:
  sudo tapbox-lib add Fortellinger Fantorangen \\
      https://radio.nrk.no/podkast/fantorangenfortellinger
  sudo tapbox-lib add Musikk Barnesanger \\
      "https://open.spotify.com/playlist/..."
  sudo tapbox-lib add Lydbøker "Lydbok X" /var/lib/tapbox/local/lydbok-x oldest

'order' controls playback/menu direction for multi-episode links:
auto = the service's natural order (NRK podcasts newest first, series and
folders oldest first, RSS as the feed lists them).
"""

import hashlib
import json
import os
import re
import sys

LIB_FILE = os.environ.get("TAPBOX_LIBRARY", "/etc/tapbox/library.json")
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
              "  sudo tapbox-lib add <section> <name> <link>")
        return
    for s in lib["sections"]:
        print(f"{s['name']}")
        for e in s["entries"]:
            order = "" if e.get("order", "auto") == "auto" else f"  [{e['order']}]"
            print(f"  {e['id']}  {e['name']}{order}")
            print(f"            {e['target']}")


def cmd_add(lib, args):
    if len(args) < 3:
        sys.exit("usage: tapbox-lib add <section> <name> <target> [auto|newest|oldest]")
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
        sys.exit("usage: tapbox-lib rm <entry-id>")
    for s in lib["sections"]:
        for e in s["entries"]:
            if e["id"] == args[0]:
                s["entries"].remove(e)
                lib["sections"] = [x for x in lib["sections"] if x["entries"]]
                save(lib)
                print(f"removed {e['name']}")
                return
    sys.exit(f"no entry {args[0]} (see: tapbox-lib list)")


def cmd_order(lib, args):
    if len(args) != 2 or args[1] not in ORDERS:
        sys.exit("usage: tapbox-lib order <entry-id> auto|newest|oldest")
    for _s, e in entries(lib):
        if e["id"] == args[0]:
            e["order"] = ORDERS[args[1]]
            save(lib)
            print(f"{e['name']}: order = {e['order']}")
            return
    sys.exit(f"no entry {args[0]} (see: tapbox-lib list)")


def main():
    cmds = {"list": cmd_list, "add": cmd_add, "rm": cmd_rm, "order": cmd_order}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)
    cmds[sys.argv[1]](load(), sys.argv[2:])


if __name__ == "__main__":
    main()
