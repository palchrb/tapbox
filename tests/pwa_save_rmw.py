#!/usr/bin/env python3
"""Gate the PWA's read-modify-write saves: every saveLibrary() call must
re-fetch the server's current library and apply only its OWN change
(keyed by stable entry/section ids) before PUTting — never PUT the
tab's whole in-memory copy. The field bug (2026-07-19): a client with a
stale copy (old cached app.js, a second device, a suspended phone PWA)
wiped every edit made elsewhere since it loaded, so the Coco album's
pre-cache flag "never stuck" no matter how many times it was toggled.

Runs the real pi/web/app.js under node with a throwaway DOM shim and an
in-memory /library server, then replays the two-client scenario."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
"use strict";
const fs = require("fs");
process.on("unhandledRejection", () => {});  // background pollStatus noise

/* --- in-memory tapboxd: GET/PUT /library only ------------------------- */
let server = null;
global.fetch = async (path, opts = {}) => {
  const ok = (obj) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(obj)) });
  if (path === "/library" && opts.method === "PUT") {
    server = JSON.parse(opts.body);
    return ok(server);
  }
  if (path === "/library") return ok(server);
  return ok({});
};

/* --- DOM shim: every property/call resolves to more shim -------------- */
const anything = new Proxy(function () {}, {
  get(t, p) {
    if (typeof p === "symbol" || p === "toString") return () => "";
    return anything;
  },
  apply() { return anything; },
  construct() { return anything; },
  set() { return true; },
});
global.document = anything;
global.window = global;
global.navigator = anything;
global.setTimeout = () => 0;
global.setInterval = () => 0;
global.clearTimeout = () => {};
global.clearInterval = () => {};

/* --- load the real app.js, export its internals ----------------------- */
let src = fs.readFileSync(process.env.APPJS, "utf8");
src += "\n;globalThis.__t = { saveLibrary, libEntry," +
       " getLIB: () => LIB, setLIB: (v) => { LIB = v; } };";
(0, eval)(src);

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const copy = (o) => JSON.parse(JSON.stringify(o));

(async () => {
  const t = globalThis.__t;
  const doc = (cocoCache, extra) => ({ version: 1, sections: [{
    id: "s1", name: "Eventyr", entries: [
      { id: "coco1", name: "Coco", target: "spotify:album:AAA",
        order: "in-order", cache: cocoCache, resume: true },
      { id: "e2", name: "Kardemomme", target: "spotify:playlist:BBB",
        order: "in-order", cache: 0, resume: true },
      ...(extra ? [extra] : []),
    ]}]});

  // 1. the field bug: client A loaded before client B toggled Coco's
  // pre-cache ON; A then saves an UNRELATED change. B's toggle must
  // survive (the old whole-document PUT reverted it to 0 every time).
  server = doc(0);
  t.setLIB(copy(server));         // A's stale in-memory copy
  server = doc(-1);               // B toggles Coco pre-cache ON
  await t.saveLibrary((lib) => {
    const x = t.libEntry(lib, "e2"); if (x) x.order = "shuffle";
  });
  assert(server.sections[0].entries[0].cache === -1,
         "stale client's save wiped the other device's toggle");
  assert(server.sections[0].entries[1].order === "shuffle",
         "the stale client's own change was lost");
  console.log("1. stale client's unrelated save keeps the other device's toggle OK");

  // 2. the toggle itself from a stale client: entries added elsewhere
  // in the meantime must survive, and the toggle must land.
  const extra = { id: "new1", name: "Ny", target: "spotify:album:CCC",
                  order: "in-order", cache: 0, resume: true };
  server = doc(0);
  t.setLIB(copy(server));         // A loads
  server = doc(0, extra);         // B adds an entry A never saw
  await t.saveLibrary((lib) => {
    const x = t.libEntry(lib, "coco1"); if (x) x.cache = -1;
  });
  assert(server.sections[0].entries.length === 3,
         "entry added on another device was erased");
  assert(server.sections[0].entries[0].cache === -1, "the toggle was lost");
  console.log("2. toggle from a stale client lands without erasing unseen entries OK");

  // 3. after a save, LIB rebinds to the server's response (fresh doc)
  assert(t.getLIB().sections[0].entries.length === 3,
         "LIB still holds the stale copy after save");
  console.log("3. LIB rebinds to the server's response after save OK");

  // 4. a mutator whose target id is gone (deleted elsewhere) is a
  // harmless no-op PUT of the fresh doc — nothing resurrects.
  server = doc(-1);
  t.setLIB(copy(doc(0, extra)));  // stale copy still HAS the extra entry
  await t.saveLibrary((lib) => {
    const x = t.libEntry(lib, "new1"); if (x) x.cache = 5;
  });
  assert(server.sections[0].entries.length === 2,
         "a deleted entry was resurrected from the stale copy");
  assert(server.sections[0].entries[0].cache === -1, "fresh state disturbed");
  console.log("4. mutator for a since-deleted entry is a harmless no-op OK");

  console.log("PWA SAVE RMW OK — every save re-reads the document and applies " +
              "one keyed change; last-writer-wins wipes are gone.");
})().catch((e) => { console.error("FAIL: " + e.message); process.exit(1); });
"""

env = dict(os.environ, APPJS=os.path.join(REPO, "pi", "web", "app.js"))
try:
    r = subprocess.run(["node", "-e", HARNESS], env=env,
                       capture_output=True, text=True, timeout=30)
except FileNotFoundError:
    print("PWA SAVE RMW SKIPPED — node not installed on this box "
          "(the PWA runs on the parent's phone, not the Pi)")
    sys.exit(0)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
