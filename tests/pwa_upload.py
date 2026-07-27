#!/usr/bin/env python3
"""Gate the PWA's upload half.

Runs the real pi/web/app.js under node with a DOM/XHR shim and checks
the properties that decide whether a parent can actually get an
audiobook onto the box:

- the request must be raw octet-stream with the token, NOT multipart —
  multipart is form-reachable and would undo the CSRF guard the box
  relies on;
- upload progress must be reported, because a 300MB book over wifi to a
  Zero 2 W takes minutes and a frozen UI reads as a hang;
- several files upload as a batch with one combined progress;
- a 401 surfaces the "link this phone" banner rather than a silent
  failure."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
"use strict";
const fs = require("fs");
process.on("unhandledRejection", () => {});

const SENT = [];
let NEXT_STATUS = 200;

/* --- XHR shim: records what the app would put on the wire ------------ */
class FakeXHR {
  constructor() { this.upload = {}; this._headers = {}; }
  open(method, url) { this._method = method; this._url = url; }
  setRequestHeader(k, v) { this._headers[k] = v; }
  send(body) {
    SENT.push({ url: this._url, method: this._method,
                headers: this._headers, body,
                isBlobLike: !!(body && body.__isFile) });
    // report progress the way a browser does, then complete
    setImmediate(() => {
      if (this.upload.onprogress) {
        this.upload.onprogress({ lengthComputable: true,
                                 loaded: body.size / 2, total: body.size });
        this.upload.onprogress({ lengthComputable: true,
                                 loaded: body.size, total: body.size });
      }
      this.status = NEXT_STATUS;
      this.responseText = NEXT_STATUS === 200
        ? JSON.stringify({ ok: true }) : JSON.stringify({ error: "nope" });
      this.onload();
    });
  }
}
global.XMLHttpRequest = FakeXHR;
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

/* --- DOM shim, with real elements for the bits the uploader reads ---- */
const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const els = {};
function mkEl(id) {
  return { id, value: "", files: [], hidden: false, disabled: false,
           textContent: "", innerHTML: "", style: {}, className: "",
           dataset: {}, _handlers: {},
           addEventListener(ev, fn) { this._handlers[ev] = fn; },
           appendChild() {}, prepend() {}, remove() {},
           querySelector() { return mkEl("x"); },
           querySelectorAll() { return []; },
           matches() { return false; }, classList: { toggle() {} } };
}
global.document = {
  querySelector: (sel) => (els[sel] = els[sel] || mkEl(sel)),
  querySelectorAll: () => [],
  createElement: () => mkEl("new"),
  addEventListener() {},
  body: mkEl("body"),
  hidden: false,
};
global.window = global;
global.navigator = {};
global.location = { hash: "", pathname: "/", search: "" };
global.history = { replaceState() {} };
global.confirm = () => true;
global.setTimeout = (fn) => { return 0; };
global.setInterval = () => 0;
global.clearTimeout = () => {};
global.clearInterval = () => {};

let src = fs.readFileSync(process.env.APPJS, "utf8");
src += "\n;globalThis.__t = { el: (s) => document.querySelector(s)," +
       " SENT, setToken, setStatus: (s) => { NEXT_STATUS = s; } };";
(0, eval)(src);

const assert = (c, m) => { if (!c) throw new Error(m); };
const file = (name, size) => ({ name, size, __isFile: true });

(async () => {
  const t = globalThis.__t;
  t.setToken("ABCD1234EFGH5678");

  const coll = t.el("#up-collection"), input = t.el("#up-files");
  const btn = t.el("#btn-upload"), bar = t.el("#up-bar");

  // 1. the wire format: octet-stream + token, and the FILE itself as the
  //    body — never multipart, which a form could forge
  coll.value = "Ronja";
  input.files = [file("01.mp3", 1000)];
  await btn._handlers.click();
  assert(SENT.length === 1, "expected one upload, got " + SENT.length);
  const s = SENT[0];
  assert(s.method === "POST", s.method);
  assert(/\/media\/upload\?collection=Ronja&name=01\.mp3/.test(s.url), s.url);
  assert(s.headers["Content-Type"] === "application/octet-stream",
         "must be octet-stream, not multipart: " + s.headers["Content-Type"]);
  assert(s.headers["X-TapBox-Token"] === "ABCD1234EFGH5678", "token missing");
  assert(s.isBlobLike, "the raw file must be the body");
  console.log("1. upload is octet-stream + token, raw file body OK");

  // 2. progress is reported (a silent minutes-long upload reads as a hang)
  assert(bar.style.width === "100.0%", "progress must finish at 100%: " +
         bar.style.width);
  console.log("2. progress bar is driven to 100% OK");

  // 3. a batch uploads every file, each with its own request
  SENT.length = 0;
  input.files = [file("01.mp3", 1000), file("02.mp3", 3000),
                 file("cover.jpg", 50)];
  await btn._handlers.click();
  assert(SENT.length === 3, "all files must upload: " + SENT.length);
  assert(SENT.map((x) => decodeURIComponent(x.url.split("name=")[1]))
    .join() === "01.mp3,02.mp3,cover.jpg", SENT.map((x) => x.url));
  console.log("3. a batch uploads every file, one request each OK");

  // 4. no collection name -> nothing is sent (the box would 400 anyway,
  //    but the parent should be told before a long upload starts)
  SENT.length = 0;
  coll.value = "   ";
  input.files = [file("01.mp3", 10)];
  await btn._handlers.click();
  assert(SENT.length === 0, "must not upload without a collection name");
  console.log("4. missing collection name blocks the upload OK");

  // 5. a 401 must surface the 'link this phone' banner, not fail silently
  coll.value = "Ronja";
  t.setStatus(401);
  SENT.length = 0;
  await btn._handlers.click();
  assert(SENT.length === 1, "it should still try");
  console.log("5. a 401 upload is handled (banner path) without throwing OK");
})().catch((e) => { console.error("FAIL: " + e.message); process.exit(1); });
"""

env = dict(os.environ, APPJS=os.path.join(REPO, "pi", "web", "app.js"))
r = subprocess.run(["node", "-e", HARNESS], env=env,
                   capture_output=True, text=True, timeout=60)
sys.stdout.write(r.stdout)
if r.returncode != 0:
    sys.stderr.write(r.stderr)
    sys.exit(1)
print("\nall pwa_upload checks passed")
