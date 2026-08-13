#!/usr/bin/env python3
"""Gate the PWA's Spotify queue card.

The queue card used to hide for every spotify target ('spotify = leaf,
no listing') — written before the fork could list a context's songs.
Now it renders them like podcast episodes (via /expand tracks=1), a tap
plays the song (/play {target, episode: <track uri>}), and the playing
row is marked from /status spotify.track_uri (mpv keeps episode_id).

Runs the real pi/web/app.js under node with a recording DOM shim, like
pwa_save_rmw.py."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
"use strict";
const fs = require("fs");
process.on("unhandledRejection", () => {});

/* --- recording DOM shim ------------------------------------------------ */
function makeEl(tag) {
  const el = {
    tag, children: [], listeners: {}, dataset: {}, className: "",
    hidden: false, _text: "", value: "", style: {},
    matches: () => false,
    setAttribute() {}, removeAttribute() {}, focus() {}, remove() {},
    insertBefore(c) { el.children.unshift(c); return c; },
    classes: new Set(),
    set textContent(v) { el._text = v; el.children = []; },
    get textContent() { return el._text; },
    appendChild(c) { el.children.push(c); return c; },
    append(...cs) { el.children.push(...cs); },
    addEventListener(ev, fn) { el.listeners[ev] = fn; },
    classList: {
      toggle(name, on) {
        if (on) el.classes.add(name); else el.classes.delete(name);
      },
      add(n) { el.classes.add(n); },
      remove(n) { el.classes.delete(n); },
    },
  };
  created.push(el);
  return el;
}
const created = [];
const byId = {};
global.document = {
  createElement: makeEl,
  querySelector(sel) { return byId[sel] || (byId[sel] = makeEl(sel)); },
  querySelectorAll(sel) {
    if (sel === ".queue-ep")
      return created.filter((e) => e.className === "entry queue-ep");
    return [];
  },
  addEventListener() {},
};
global.window = global;
global.navigator = {};
global.location = { hash: "", pathname: "/", search: "", origin: "http://t" };
global.history = { replaceState() {} };
global.localStorage = { getItem: () => "", setItem: () => {},
                        removeItem: () => {} };
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return 0; };
global.setInterval = () => 0;
global.clearTimeout = () => {};
global.clearInterval = () => {};

/* --- in-memory vibbd ------------------------------------------------- */
const SPOT = "https://open.spotify.com/playlist/0hg";
const calls = [];
global.fetch = async (path, opts = {}) => {
  calls.push({ path, opts });
  const ok = (obj) => ({ ok: true,
                         json: async () => JSON.parse(JSON.stringify(obj)) });
  if (path.startsWith("/library"))
    return ok({ version: 1, sections: [{ id: "s", name: "S", entries: [
      { id: "pl1", name: "80s", target: SPOT, order: "auto",
        cache: 0, resume: true }] }] });
  if (path.startsWith("/expand"))
    return ok({ kind: "spotify", pending: global.EXPAND_PENDING || false,
                episodes: global.EXPAND_EMPTY ? [] : [
      { id: "spotify:track:a", title: "Blue Monday '88 — New Order",
        url: "spotify:track:a", cached: false },
      { id: "spotify:track:c", title: "Shout", url: "spotify:track:c",
        cached: false }] });
  return ok({});
};

let src = fs.readFileSync(process.env.APPJS, "utf8");
src += "\n;globalThis.__t = { loadQueue, markQueuePlaying };";
(0, eval)(src);

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

(async () => {
  const t = globalThis.__t;
  const card = global.document.querySelector("#queue-card");

  // 1. a spotify target renders song rows (the old guard hid the card)
  await t.loadQueue(SPOT);
  const expand = calls.find((c) => c.path.startsWith("/expand"));
  assert(expand, "spotify target must hit /expand now");
  assert(expand.path.includes("tracks=1"),
         "the fork listing needs tracks=1: " + expand.path);
  assert(expand.path.includes("id=pl1"),
         "library entry preferred (play order): " + expand.path);
  const rows = global.document.querySelectorAll(".queue-ep");
  assert(rows.length === 2, "two song rows expected, got " + rows.length);
  assert(card.hidden === false, "the card must show for spotify");
  assert(rows[0].children[0].children[0]._text ===
         "Blue Monday '88 — New Order", "row title wrong");
  assert(rows[0].children[0].children[1]._text === "",
         "spotify rows must not claim offline");
  console.log("1. spotify target renders song rows via tracks=1 OK");

  // 2. tapping a row plays that song in its context
  await rows[1].listeners.click();
  const play = calls.find((c) => c.path === "/play" &&
                                 c.opts.method === "POST");
  assert(play, "row tap must POST /play");
  const body = JSON.parse(play.opts.body);
  assert(body.target === SPOT && body.episode === "spotify:track:c",
         "tap must play {target, episode:<track uri>}: " +
         play.opts.body);
  console.log("2. row tap plays the song in context OK");

  // 3. the playing row is marked from spotify.track_uri (fallback when
  //    episode_id is null — mpv keeps its own field)
  t.markQueuePlaying(null || "spotify:track:c");
  assert(rows[1].classes.has("playing") && !rows[0].classes.has("playing"),
         "spotify track_uri must mark the playing row");
  console.log("3. playing row marked from track_uri OK");

  // 4. the source wires that fallback (a plain episode_id call would
  //    pass this harness while the page never sends track_uri)
  assert(/markQueuePlaying\(st\.episode_id \|\|\s*\(st\.spotify && st\.spotify\.track_uri\)\)/.test(src),
         "status poll must fall back to st.spotify.track_uri");
  console.log("4. status poll falls back to spotify.track_uri OK");

  // 5. no target still hides the card
  await t.loadQueue(null);
  assert(card.hidden === true, "no target must hide the card");
  console.log("5. no target: card hidden OK");

  // 6. a pending (settle-timeout) listing schedules EXACTLY ONE delayed
  //    re-fetch per target — an empty first load used to pin an empty
  //    card until the target changed; a never-settling context must not
  //    loop either
  global.EXPAND_PENDING = true;
  global.EXPAND_EMPTY = true;
  timers.length = 0;
  await t.loadQueue(SPOT);
  let retries = timers.filter((x) => x.ms === 4000);
  assert(retries.length === 1, "one retry must be scheduled, got "
         + retries.length);
  assert(card.hidden === true, "empty pending listing shows no card yet");
  await t.loadQueue(SPOT);  // still pending on the retry
  retries = timers.filter((x) => x.ms === 4000);
  assert(retries.length === 1, "pending retries must never stack/loop");
  // the retry callback only refires while the target is still current
  global.EXPAND_PENDING = false;
  global.EXPAND_EMPTY = false;
  const before = created.length;
  await retries[0].fn();  // the callback returns loadQueue's promise
  const fresh = created.slice(before)
    .filter((e) => e.className === "entry queue-ep");
  assert(fresh.length === 2,
         "the completed retry must render the rows, got " + fresh.length);
  assert(card.hidden === false, "…and show the card");
  console.log("6. pending listing: one bounded retry, completes to rows OK");

  console.log("PWA SPOTIFY QUEUE OK — songs listed, tappable, marked.");
})().catch((e) => { console.error("FAIL: " + e.message); process.exit(1); });
"""

env = dict(os.environ,
           APPJS=os.path.join(REPO, "pi", "web", "app.js"))
r = subprocess.run(["node", "-e", HARNESS], env=env,
                   capture_output=True, text=True, timeout=60)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
