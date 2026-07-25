#!/usr/bin/env python3
"""Gate the PWA's half of the API token (SECURITY.md Model B).

Runs the real pi/web/app.js under node with a DOM/localStorage shim and
a fake tapboxd, then checks the four things that decide whether a parent
can actually use the box after the gate landed:

- the token rides along on every request once linked;
- scanning the box's QR (#t=... in the URL) links the phone, and the
  secret is stripped from the URL bar so it can't sit in history or a
  screenshot;
- a 401 raises a clear 'not linked' error, and a token the box no longer
  recognises is DROPPED (so the UI says 'link me' instead of silently
  failing every privileged action forever);
- an unlinked phone still controls playback — the box must never look
  dead just because nobody scanned the code."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
"use strict";
const fs = require("fs");
process.on("unhandledRejection", () => {});

/* --- fake tapboxd: mirrors the real gate ------------------------------ */
const SAFE = new Set(["/status", "/playpause", "/next", "/prev", "/pause"]);
let TOKEN_ON_BOX = "ABCD1234EFGH5678";
const seen = [];
global.fetch = async (path, opts = {}) => {
  const hdr = (opts.headers || {})["X-TapBox-Token"] || "";
  seen.push({ path, token: hdr });
  const json = (obj, ok, status) => ({
    ok, status: status || (ok ? 200 : 400), json: async () => obj });
  if (SAFE.has(path)) return json({ ok: true }, true);
  if (!hdr) return json({ error: "not linked", code: "token_required" },
                        false, 401);
  if (hdr !== TOKEN_ON_BOX)
    return json({ error: "not linked", code: "token_invalid" }, false, 401);
  return json({ ok: true }, true);
};

/* --- DOM shim (same trick as pwa_save_rmw) with a real localStorage --- */
const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
let URL_NOW = "/";
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
global.location = { hash: process.env.HASH || "", pathname: "/", search: "" };
global.history = { replaceState: (a, b, url) => { URL_NOW = url; } };
global.setTimeout = () => 0;
global.setInterval = () => 0;
global.clearTimeout = () => {};
global.clearInterval = () => {};

let src = fs.readFileSync(process.env.APPJS, "utf8");
src += "\n;globalThis.__t = { api, setToken, normToken," +
       " getTOKEN: () => TOKEN, seen, store, urlNow: () => URL_NOW," +
       " setBoxToken: (v) => { TOKEN_ON_BOX = v; } };";
(0, eval)(src);

const assert = (c, m) => { if (!c) throw new Error(m); };

(async () => {
  const t = globalThis.__t;

  if (process.env.HASH) {
    // 2. QR landing: the box screen's #t=... linked this phone...
    assert(t.getTOKEN() === "ABCD1234EFGH5678",
           "scanning the box QR must link the phone: " + t.getTOKEN());
    assert(store["tapbox.token"] === "ABCD1234EFGH5678",
           "the token must persist for the next visit");
    // ...and the secret must not be left in the URL bar / history
    assert(!String(t.urlNow()).includes("ABCD"),
           "the token must be stripped from the URL: " + t.urlNow());
    console.log("2. QR landing links the phone and strips the token from the URL OK");

    // 3. a privileged call now carries the token and succeeds
    await t.api("/system/shutdown", { method: "POST" });
    const last = t.seen[t.seen.length - 1];
    assert(last.token === "ABCD1234EFGH5678", "token must be sent");
    console.log("3. privileged call carries the token OK");

    // 4. the box rotates its token (parent pressed A on the screen):
    //    the stale one must be DROPPED, so the UI can say 'link me'
    //    instead of failing silently forever.
    t.setBoxToken("ZZZZ9999ZZZZ9999");
    let code = "";
    try { await t.api("/system/wifi", { method: "POST" }); }
    catch (e) { code = e.code; }
    assert(code === "token_invalid", "401 must surface a code: " + code);
    assert(t.getTOKEN() === "", "a rejected token must be dropped");
    assert(!("tapbox.token" in store), "and cleared from storage");
    console.log("4. rotated-away token is dropped (UI can prompt to re-link) OK");
    return;
  }

  // 1. a phone that never scanned anything: no token is sent, playback
  //    still works, and a privileged call fails with a clear reason.
  assert(t.getTOKEN() === "", "a fresh phone starts unlinked");
  await t.api("/playpause", { method: "POST" });
  assert(t.seen[t.seen.length - 1].token === "", "no token to send");
  console.log("1. unlinked phone: playback works with no token OK");

  let err = null;
  try { await t.api("/system/shutdown", { method: "POST" }); }
  catch (e) { err = e; }
  assert(err && err.code === "token_required", "expected token_required");
  assert(/isn't linked/.test(err.message), "message must explain: " + err.message);
  console.log("1b. unlinked phone: privileged call fails with 'not linked' OK");

  // 1c. typing the token by hand (the fallback when a QR won't scan),
  //     in the dashed lowercase form printed under the QR
  t.setToken("abcd-1234-efgh-5678");
  assert(t.getTOKEN() === "ABCD1234EFGH5678", "typed form must normalize");
  await t.api("/system/shutdown", { method: "POST" });
  assert(t.seen[t.seen.length - 1].token === "ABCD1234EFGH5678");
  console.log("1c. hand-typed dashed/lowercase token normalizes and works OK");

  // 1d. Crockford confusables a parent misreads off a 240px screen
  assert(t.normToken("OIL") === "011", "O->0, I/L->1");
  console.log("1d. O/I/L confusables fold to 0/1/1 OK");
})().catch((e) => { console.error("FAIL: " + e.message); process.exit(1); });
"""


def run(hash_value=""):
    env = dict(os.environ, APPJS=os.path.join(REPO, "pi", "web", "app.js"),
               HASH=hash_value)
    r = subprocess.run([("node"), "-e", HARNESS], env=env,
                       capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(1)


run()                       # a phone that never scanned the QR
run("#t=ABCD-1234-EFGH-5678")  # a phone that just scanned it
print("\nall pwa_token checks passed")
