/* TapBox parent PWA — a thin client of the tapboxd API (same origin). */
"use strict";

const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

/* --- tabs ---------------------------------------------------------------- */

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) =>
      b.classList.toggle("active", b === btn));
    ["player", "library", "settings"].forEach((t) => {
      $(`#tab-${t}`).hidden = t !== btn.dataset.tab;
    });
    if (btn.dataset.tab === "library") loadLibrary();
    if (btn.dataset.tab === "settings") { loadSettings(); loadSystem(); loadBt(); }
  });
});

/* --- player -------------------------------------------------------------- */

function fmtTime(s) {
  if (s == null) return "–";
  s = Math.floor(s);
  const m = Math.floor(s / 60) % 60, h = Math.floor(s / 3600);
  const mm = h ? String(m).padStart(2, "0") : m;
  return (h ? `${h}:` : "") + `${mm}:${String(s % 60).padStart(2, "0")}`;
}

let volTouched = 0;

// Last polled playback state; a local 500ms ticker interpolates the
// progress display between the 2s polls so seconds count up smoothly.
let np = { position: null, duration: null, playing: false, at: 0 };

function renderProgress() {
  let pos = np.position;
  if (pos != null && np.playing) pos += (Date.now() - np.at) / 1000;
  if (pos != null && np.duration != null) pos = Math.min(pos, np.duration);
  const frac = pos && np.duration ? Math.min(1, pos / np.duration) : 0;
  $("#np-bar").style.width = `${frac * 100}%`;
  $("#np-pos").textContent = fmtTime(pos);
  $("#np-dur").textContent =
    np.position != null && np.duration == null ? "live" : fmtTime(np.duration);
}

let queueTarget;   // undefined = never loaded; null = no queue
let currentTarget = null;  // what /status says is (or would be) playing

async function loadQueue(target) {
  queueTarget = target;
  const card = $("#queue-card");
  const wrap = $("#queue");
  wrap.textContent = "";
  if (!target || target.includes("spotify")) {  // spotify = leaf, no listing
    card.hidden = true;
    return;
  }
  try {
    // Prefer the library entry: /expand?id applies its play order,
    // which is the order the box actually queues in.
    let url = `/expand?target=${encodeURIComponent(target)}`;
    try {
      const lib = await api("/library");
      for (const s of lib.sections) for (const e of s.entries) {
        if (e.target === target) url = `/expand?id=${encodeURIComponent(e.id)}`;
      }
    } catch (e) { /* no library — fall back to target expand */ }
    const r = await api(url);
    const eps = r.episodes || [];
    for (const ep of eps) {
      const row = document.createElement("div");
      row.className = "entry queue-ep";
      row.dataset.episode = ep.id || "";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = ep.title || ep.id || "?";
      const sub = document.createElement("small");
      sub.textContent = ep.cached ? "✓ offline" : "";
      info.append(name, sub);
      row.appendChild(info);
      row.addEventListener("click", async () => {
        try {
          await api("/play", { method: "POST",
            body: { target, episode: ep.id } });
          toast(`Starting ${ep.title || "episode"} …`);
        } catch (e) { toast(e.message); }
      });
      wrap.appendChild(row);
    }
    card.hidden = eps.length === 0;
  } catch (e) {
    card.hidden = true;
  }
}

function markQueuePlaying(episodeId) {
  for (const row of document.querySelectorAll(".queue-ep")) {
    row.classList.toggle("playing",
      !!episodeId && row.dataset.episode === episodeId);
  }
}

async function pollStatus() {
  try {
    const st = await api("/status");
    $("#np-title").textContent = st.title || "Nothing playing";
    const artists = (st.spotify && st.spotify.playing
      ? (st.spotify.artists || []).join(", ") : "");
    $("#np-sub").textContent = artists || (st.source ? `source: ${st.source}` : "");
    const art = $("#np-art");
    if (st.artwork) {
      const src = st.artwork.startsWith("http")
        ? st.artwork : `/artwork?path=${encodeURIComponent(st.artwork)}`;
      if (art.dataset.src !== src) {
        // decode off-screen and swap only when ready — no blank flash
        art.dataset.src = src;
        const pre = new Image();
        pre.onload = () => {
          if (art.dataset.src === src) { art.src = src; art.hidden = false; }
        };
        pre.src = src;
      } else {
        art.hidden = false;
      }
    } else if (!st.title) {
      // drop the art only when there is genuinely nothing on; a null
      // artwork WITH a title is a transition blip — keep the last image
      art.hidden = true;
      art.dataset.src = "";
    }
    np = { position: st.position, duration: st.duration,
           playing: !!st.playing, at: Date.now() };
    renderProgress();
    $("#btn-play").textContent = st.playing ? "⏸" : "▶";
    currentTarget = st.target || null;
    if (st.target !== queueTarget) loadQueue(st.target);
    markQueuePlaying(st.episode_id);
    $("#btn-shuffle").classList.toggle("on", !!st.shuffle);
    $("#btn-shuffle").dataset.on = st.shuffle ? "1" : "";
    const out = document.querySelector(`input[name=output][value=${st.output}]`);
    if (out) out.checked = true;
  } catch (e) { /* box offline — keep last view */ }
  try {
    if (Date.now() - volTouched > 3000) {
      const v = await api("/volume");
      if (v.volume != null) {
        $("#volume").value = v.volume;
        $("#vol-label").textContent = `${v.volume}%`;
      }
    }
  } catch (e) { /* ignore */ }
}

$("#btn-play").addEventListener("click", () => api("/playpause", { method: "POST", body: {} }).then(pollStatus).catch((e) => toast(e.message)));
$("#btn-next").addEventListener("click", () => api("/next", { method: "POST", body: {} }).catch((e) => toast(e.message)));
$("#btn-prev").addEventListener("click", () => api("/prev", { method: "POST", body: {} }).catch((e) => toast(e.message)));
$("#btn-fresh").addEventListener("click", async () => {
  if (!currentTarget) { toast("Nothing to restart"); return; }
  if (!confirm(
    "Play from the beginning? The saved position is cleared.")) return;
  try {
    await api("/play", { method: "POST",
      body: { target: currentTarget, fresh: true } });
    toast("Starting from the beginning …");
  } catch (e) { toast(e.message); }
});

$("#btn-stop").addEventListener("click", () => api("/stop", { method: "POST", body: {} }).then(pollStatus).catch((e) => toast(e.message)));
$("#btn-shuffle").addEventListener("click", async () => {
  const enable = !$("#btn-shuffle").dataset.on;
  try {
    const r = await api("/shuffle", { method: "POST", body: { enabled: enable } });
    if (r.routed == null) {
      toast("Nothing to shuffle");
    } else {
      toast(enable ? "Shuffle on" : "Shuffle off");
    }
    pollStatus();
  } catch (e) { toast(e.message); }
});

$("#volume").addEventListener("input", () => {
  volTouched = Date.now();
  const v = Number($("#volume").value);
  $("#vol-label").textContent = `${v}%`;
  clearTimeout(window._volTimer);
  window._volTimer = setTimeout(() => {
    api("/volume", { method: "POST", body: { volume: v } })
      .then((r) => { if (r.volume != null && r.volume !== v) {
        $("#volume").value = r.volume;           // clamped by the cap
        $("#vol-label").textContent = `${r.volume}%`;
        toast(`Volume cap: ${r.volume}%`);
      } })
      .catch((e) => toast(e.message));
  }, 250);
});

document.querySelectorAll("input[name=output]").forEach((r) => {
  r.addEventListener("change", async () => {
    try {
      const res = await api("/output", { method: "POST", body: { device: r.value } });
      toast(res.warning || (res.spotify_restarted
        ? "Switching output (Spotify restarts …)" : "Audio output switched"),
        res.warning ? 9000 : 2500);
    } catch (e) { toast(e.message); }
  });
});

/* --- library ------------------------------------------------------------- */

let LIB = { version: 1, sections: [] };

const ORDER_LABEL = {
  auto: "auto", newest_first: "newest first", oldest_first: "oldest first",
};
const CACHE_OPTIONS = [0, 5, 10, 25, 50];
const isSpotify = (t) => /open\.spotify\.com|spotify:|spotify\.link\//.test(t);
const isLocal = (t) => t.startsWith("/");

async function loadLibrary() {
  LIB = await api("/library");
  const wrap = $("#sections");
  wrap.textContent = "";
  $("#section-names").textContent = "";
  for (const s of LIB.sections) {
    const opt = document.createElement("option");
    opt.value = s.name;
    $("#section-names").appendChild(opt);

    const card = document.createElement("div");
    card.className = "card";
    const h = document.createElement("h2");
    h.textContent = s.name;
    card.appendChild(h);
    for (const e of s.entries) {
      card.appendChild(entryRow(e));
    }
    wrap.appendChild(card);
  }
  if (!LIB.sections.length) {
    wrap.innerHTML = "<div class='card'><p>The library is empty — add the first link below.</p></div>";
  }
}

function entryRow(e) {
  const row = document.createElement("div");
  row.className = "entry";

  const info = document.createElement("div");
  info.className = "entry-info";
  const name = document.createElement("strong");
  name.textContent = e.name;
  const target = document.createElement("small");
  target.textContent = e.target;
  info.append(name, target);

  const order = document.createElement("select");
  for (const [val, label] of Object.entries(ORDER_LABEL)) {
    const o = document.createElement("option");
    o.value = val; o.textContent = label;
    if (e.order === val) o.selected = true;
    order.appendChild(o);
  }
  order.addEventListener("change", async () => {
    e.order = order.value;
    await saveLibrary();
    toast(`${e.name}: ${ORDER_LABEL[e.order]}`);
  });

  // Per-entry offline cache — not for Spotify (global setting, DRM) or
  // local folders (already offline)
  let cache = null;
  if (!isSpotify(e.target) && !isLocal(e.target)) {
    cache = document.createElement("select");
    cache.title = "Episodes kept offline";
    for (const n of CACHE_OPTIONS) {
      const o = document.createElement("option");
      o.value = String(n);
      o.textContent = n === 0 ? "no offline" : `keep ${n}`;
      if ((e.cache || 0) === n) o.selected = true;
      cache.appendChild(o);
    }
    cache.addEventListener("change", async () => {
      e.cache = Number(cache.value);
      await saveLibrary();
      toast(e.cache ? `${e.name}: keeps the newest ${e.cache} offline`
                    : `${e.name}: no offline copies`);
    });
  }

  const play = document.createElement("button");
  play.textContent = "▶";
  play.title = "Play now";
  play.addEventListener("click", async () => {
    try {
      await api("/play", { method: "POST", body: { id: e.id } });
      toast(`Playing: ${e.name}`);
    } catch (err) { toast(err.message); }
  });

  const del = document.createElement("button");
  del.textContent = "✕";
  del.title = "Remove";
  del.className = "danger";
  del.addEventListener("click", async () => {
    if (!confirm(`Remove “${e.name}” from the library?`)) return;
    for (const s of LIB.sections) {
      s.entries = s.entries.filter((x) => x.id !== e.id);
    }
    LIB.sections = LIB.sections.filter((s) => s.entries.length);
    await saveLibrary();
    loadLibrary();
  });

  const actions = document.createElement("div");
  actions.className = "entry-actions";
  actions.append(order);
  if (cache) actions.append(cache);
  actions.append(play, del);
  row.append(info, actions);
  return row;
}

async function saveLibrary() {
  LIB = await api("/library", { method: "PUT", body: LIB });
}

$("#add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const sectionName = $("#add-section").value.trim();
  const entry = {
    name: $("#add-name").value.trim(),
    target: $("#add-target").value.trim(),
    order: $("#add-order").value,
    cache: Number($("#add-cache").value),
  };
  let sec = LIB.sections.find(
    (s) => s.name.toLowerCase() === sectionName.toLowerCase());
  if (!sec) {
    sec = { name: sectionName, entries: [] };
    LIB.sections.push(sec);
  }
  sec.entries.push(entry);
  try {
    await saveLibrary();
    $("#add-name").value = $("#add-target").value = "";
    toast(`Added “${entry.name}”`);
    loadLibrary();
  } catch (e) {
    toast(e.message);
    loadLibrary(); // reload clean state
  }
});

/* --- settings + system ---------------------------------------------------- */

async function loadSettings() {
  const s = await api("/settings");
  $("#set-screen").value = String(s.screen_timeout_s);
  $("#set-brightness").value = String(s.screen_brightness);
  $("#set-cap").value = String(s.volume_cap);
  $("#set-idle").value = String(s.idle_shutdown_min);
  $("#set-spotcache").value = String(s.spotify_cache_gb);
  $("#set-resume").value = String(s.resume_on_boot);
  $("#set-wifioff").value = String(s.wifi_auto_off_min);
}

for (const [id, key] of [["#set-screen", "screen_timeout_s"],
                         ["#set-brightness", "screen_brightness"],
                         ["#set-cap", "volume_cap"],
                         ["#set-idle", "idle_shutdown_min"],
                         ["#set-spotcache", "spotify_cache_gb"],
                         ["#set-resume", "resume_on_boot"],
                         ["#set-wifioff", "wifi_auto_off_min"]]) {
  $(id).addEventListener("change", async () => {
    try {
      await api("/settings", { method: "PUT",
        body: { [key]: Number($(id).value) } });
      toast("Saved");
    } catch (e) { toast(e.message); }
  });
}

function fmtBytes(n) {
  if (n == null) return "–";
  for (const u of ["B", "kB", "MB", "GB"]) {
    if (n < 1024) return `${n.toFixed(0)} ${u}`;
    n /= 1024;
  }
  return `${n.toFixed(1)} TB`;
}

async function loadSystem() {
  const sys = await api("/system");
  // keep the header pill in sync — otherwise it shows a reading up to
  // 60s older than the settings row and the two disagree
  renderBatteryPill(sys);
  const rows = [];
  rows.push(["Battery", sys.battery == null ? "unknown"
    : `${Math.round(sys.battery)}%${sys.plugged ? " (charging)" : ""}`]);
  if (sys.battery_v != null) {
    rows.push(["Battery voltage", `${sys.battery_v.toFixed(2)} V`]);
  }
  if (sys.on_battery_s != null) {
    const h = Math.floor(sys.on_battery_s / 3600);
    const m = Math.floor((sys.on_battery_s % 3600) / 60);
    rows.push(["On battery", h ? `${h} h ${m} min` : `${m} min`]);
  }
  if (sys.disk) {
    rows.push(["SD card free", `${fmtBytes(sys.disk.free)} of ${fmtBytes(sys.disk.total)}`]);
  }
  for (const [k, v] of Object.entries(sys.caches || {})) {
    rows.push([k === "podcasts" ? "Podcast cache" : "Spotify cache", fmtBytes(v)]);
  }
  rows.push(["Wi-Fi", sys.wifi.enabled ? (sys.wifi.ssid || "on (not connected)") : "off"]);
  rows.push(["IP", sys.wifi.ip || "–"]);
  if (sys.cpu_temp != null) rows.push(["CPU temp", `${sys.cpu_temp}°C`]);
  $("#spotify-current").textContent = sys.spotify_user
    ? `Logged in as ${sys.spotify_user}`
    : "Not logged in — pick the box under Devices in the Spotify app";
  rows.push(["Box", sys.hostname]);
  const dl = $("#sysinfo");
  dl.textContent = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  $("#btn-wifi").textContent = sys.wifi.enabled ? "Turn Wi-Fi off" : "Turn Wi-Fi on";
  $("#btn-wifi").dataset.enabled = sys.wifi.enabled ? "1" : "";
  $("#wifi-current").textContent = sys.wifi.hotspot
    ? `Setup hotspot active: ${sys.wifi.hotspot_ssid}`
    : sys.wifi.enabled
      ? (sys.wifi.ssid ? `Connected to ${sys.wifi.ssid} (${sys.wifi.ip || "no IP"})`
                       : "On — not connected to any network")
      : "Wi-Fi is off";
  $("#btn-hotspot").textContent = sys.wifi.hotspot
    ? "Stop hotspot" : "Setup hotspot";
  $("#btn-hotspot").dataset.on = sys.wifi.hotspot ? "1" : "";
}

$("#btn-hotspot").addEventListener("click", async () => {
  const enable = !$("#btn-hotspot").dataset.on;
  if (enable && !confirm(
    "Start the setup hotspot? The box LEAVES the current network — connect "
    + "your phone to the hotspot (see the name/password in the next toast) "
    + "to reach this page again.")) return;
  try {
    const r = await api("/wifi/hotspot", { method: "POST", body: { enabled: enable } });
    toast(enable && r.ok
      ? `Hotspot: ${r.ssid} — password: ${r.password}` : enable
        ? (r.output || "Hotspot failed") : "Hotspot stopped", 15000);
    loadSystem();
  } catch (e) {
    toast(enable
      ? "Lost contact — the box is now the hotspot; join it and reload."
      : e.message, 10000);
  }
});

$("#btn-wifi").addEventListener("click", async () => {
  const enable = !$("#btn-wifi").dataset.enabled;
  if (!enable && !confirm(
    "Turn Wi-Fi off? This page loses contact with the box until Wi-Fi is back on (via the screen or a reboot).")) return;
  try {
    await api("/system/wifi", { method: "POST", body: { enabled: enable } });
    toast(enable ? "Wi-Fi on" : "Wi-Fi off");
    loadSystem();
  } catch (e) { toast(e.message); }
});

$("#wifi-add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ssid = $("#wifi-add-ssid").value.trim();
  const pass = $("#wifi-add-pass").value;
  if (pass && (pass.length < 8 || pass.length > 63)) {
    toast("WPA password must be 8-63 characters"); return;
  }
  try {
    const r = await api("/wifi/add",
      { method: "POST", body: { ssid, password: pass || undefined } });
    toast(r.output || (r.ok ? "Saved" : "Failed"), 6000);
    if (r.ok) { $("#wifi-add-ssid").value = ""; $("#wifi-add-pass").value = ""; }
  } catch (e) { toast(e.message); }
});

$("#btn-wifi-reconnect").addEventListener("click", async () => {
  try {
    await api("/system/wifi", { method: "POST", body: { enabled: true } });
    toast("Wi-Fi on — reconnecting…");
    setTimeout(loadSystem, 4000);
  } catch (e) { toast(e.message); }
});

$("#btn-spotify-logout").addEventListener("click", async () => {
  if (!confirm(
    "Log the box out of Spotify? Afterwards, pick the box under Devices " +
    "in the Spotify app with the account you want.")) return;
  try {
    await api("/spotify/logout", { method: "POST", body: {} });
    toast("Logged out — pick the box in the Spotify app", 6000);
    setTimeout(loadSystem, 4000);
  } catch (e) { toast(e.message); }
});

$("#btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Shut down the box?")) return;
  await api("/system/shutdown", { method: "POST", body: {} }).catch(() => {});
  toast("Shutting down …", 10000);
});
$("#btn-restart").addEventListener("click", async () => {
  if (!confirm("Restart the box?")) return;
  await api("/system/shutdown", { method: "POST", body: { restart: true } }).catch(() => {});
  toast("Restarting …", 10000);
});

/* --- bluetooth ------------------------------------------------------------- */

async function loadBt() {
  let bt;
  try { bt = await api("/bt"); } catch (e) { return; }
  const active = bt.devices.find((d) => d.mac === bt.configured);
  $("#bt-current").textContent = bt.configured
    ? `Active: ${active ? active.name : bt.configured}` +
      (active && active.connected ? " (connected)" : " (not connected now)")
    : "No speaker selected yet.";
  const wrap = $("#bt-devices");
  wrap.textContent = "";
  for (const d of bt.devices) {
    const row = document.createElement("div");
    row.className = "entry";
    const info = document.createElement("div");
    info.className = "entry-info";
    const name = document.createElement("strong");
    name.textContent = d.name + (d.connected ? " ●" : "");
    const mac = document.createElement("small");
    mac.textContent = d.mac + (d.paired ? " · paired" : "");
    info.append(name, mac);

    const isActive = d.connected && d.mac === bt.configured;
    const use = document.createElement("button");
    use.textContent = isActive ? "Active" : "Connect";
    use.disabled = isActive;
    use.addEventListener("click", () => btAction("/bt/connect", { mac: d.mac },
      `Connecting to ${d.name} …`));

    row.append(info, use);
    if (d.connected && d.mac !== bt.configured) {
      // a device that connected on its own — hang up without forgetting
      const disc = document.createElement("button");
      disc.textContent = "Disconnect";
      disc.addEventListener("click", () =>
        btAction("/bt/disconnect", { mac: d.mac }, "Disconnecting …"));
      row.append(disc);
    }
    const forget = document.createElement("button");
    forget.textContent = "Forget";
    forget.className = "danger";
    forget.addEventListener("click", () => {
      if (confirm(`Forget “${d.name}”? This removes the pairing.`)) {
        btAction("/bt/forget", { mac: d.mac }, "Forgetting …");
      }
    });
    row.append(forget);
    wrap.appendChild(row);
  }
  $("#btn-pair").disabled = bt.pairing;
}

async function btAction(path, body, busyMsg) {
  toast(busyMsg, 60000);
  try {
    const r = await api(path, { method: "POST", body });
    toast(r.ok ? "OK" : (r.output || "Failed").split("\n").pop(), r.ok ? 2500 : 8000);
  } catch (e) {
    toast(e.message, 6000);
  }
  loadBt();
}

$("#btn-pair").addEventListener("click", async () => {
  const btn = $("#btn-pair");
  btn.disabled = true;
  btn.textContent = "Pairing …";
  await btAction("/bt/pair", {}, "Scanning and pairing the nearest speaker …");
  btn.disabled = false;
  btn.textContent = "Pair nearest";
});

$("#btn-scan").addEventListener("click", async () => {
  const btn = $("#btn-scan");
  btn.disabled = true;
  btn.textContent = "Scanning … (~25 s)";
  const wrap = $("#bt-found");
  wrap.textContent = "";
  try {
    const r = await api("/bt/scan", { method: "POST", body: {} });
    if (!r.found.length) {
      wrap.innerHTML = "<p class='dim'>No new devices found — is the speaker in pairing mode and nearby?</p>";
    }
    for (const d of r.found) {
      const row = document.createElement("div");
      row.className = "entry";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = d.name + (d.audio ? " 🔊" : "");
      const mac = document.createElement("small");
      mac.textContent = d.mac;
      info.append(name, mac);
      const pick = document.createElement("button");
      pick.textContent = "Pair and connect";
      pick.addEventListener("click", async () => {
        wrap.textContent = "";
        await btAction("/bt/connect", { mac: d.mac },
          `Pairing and connecting to ${d.name} …`);
      });
      row.append(info, pick);
      wrap.appendChild(row);
    }
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Scan for new";
});

/* --- wifi join ---------------------------------------------------------------- */

function signalBars(pct) {
  return pct > 75 ? "▂▄▆█" : pct > 50 ? "▂▄▆" : pct > 25 ? "▂▄" : "▂";
}

$("#btn-wifi-scan").addEventListener("click", async () => {
  const btn = $("#btn-wifi-scan");
  btn.disabled = true;
  btn.textContent = "Scanning …";
  const wrap = $("#wifi-list");
  wrap.textContent = "";
  try {
    const r = await api("/wifi/scan", { method: "POST", body: {} });
    if (!r.ok) {
      toast(r.output || "Scan failed", 6000);
    } else if (!r.networks.length) {
      wrap.innerHTML = "<p class='dim'>No networks found.</p>";
    }
    for (const n of r.networks) {
      const row = document.createElement("div");
      row.className = "entry";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = (n.in_use ? "✓ " : "") + n.ssid + (n.secured ? " 🔒" : "");
      const sub = document.createElement("small");
      sub.textContent = `${signalBars(n.signal)} ${n.signal}%` +
        (n.known ? " · saved" : "");
      info.append(name, sub);
      row.appendChild(info);
      if (!n.in_use) {
        const join = document.createElement("button");
        join.textContent = n.known ? "Connect" : "Join";
        join.addEventListener("click", () => wifiJoin(n));
        row.appendChild(join);
        if (n.known) {
          const forget = document.createElement("button");
          forget.textContent = "Forget";
          forget.className = "danger";
          forget.addEventListener("click", async () => {
            if (!confirm(`Forget the saved network “${n.ssid}”?`)) return;
            await api("/wifi/forget", { method: "POST", body: { ssid: n.ssid } })
              .then((res) => toast(res.ok ? "Forgotten" : res.output, 4000))
              .catch((e) => toast(e.message));
          });
          row.appendChild(forget);
        }
      }
      wrap.appendChild(row);
    }
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Scan for networks";
});

async function wifiJoin(n) {
  let password;
  if (n.secured && !n.known) {
    password = prompt(`Password for “${n.ssid}”:`);
    if (!password) return;
  }
  toast(`Joining ${n.ssid} … the box may move networks (reconnect your phone if this page stops responding)`, 60000);
  try {
    const r = await api("/wifi/connect",
      { method: "POST", body: { ssid: n.ssid, password } });
    toast(r.ok ? `Connected to ${r.ssid} (${r.ip || "getting IP …"})`
               : (r.output || "Failed").split("\n").pop(), r.ok ? 4000 : 8000);
    loadSystem();
  } catch (e) {
    toast("Lost contact — if the box joined the new network, reconnect your phone to it and reload.", 10000);
  }
}

/* --- header battery pill ---------------------------------------------------- */

function renderBatteryPill(sys) {
  const b = $("#battery");
  if (sys.battery == null) {
    b.textContent = "–";
    b.classList.remove("low");
  } else {
    b.textContent = `${Math.round(sys.battery)}%${sys.plugged ? " ⚡" : ""}`;
    b.classList.toggle("low", !sys.plugged && sys.battery <= 15);
  }
}

async function pollBattery() {
  try {
    renderBatteryPill(await api("/system"));
  } catch (e) { /* box offline */ }
}

/* --- boot ------------------------------------------------------------------ */

pollStatus();
setInterval(pollStatus, 2000);
setInterval(renderProgress, 500);
pollBattery();
setInterval(pollBattery, 60000);
