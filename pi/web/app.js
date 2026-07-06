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

async function pollStatus() {
  try {
    const st = await api("/status");
    $("#np-title").textContent = st.title || "Ingenting spiller";
    const artists = (st.spotify && st.spotify.playing
      ? (st.spotify.artists || []).join(", ") : "");
    $("#np-sub").textContent = artists || (st.source ? `kilde: ${st.source}` : "");
    const art = $("#np-art");
    if (st.artwork) {
      const src = st.artwork.startsWith("http")
        ? st.artwork : `/artwork?path=${encodeURIComponent(st.artwork)}`;
      if (art.dataset.src !== src) { art.src = src; art.dataset.src = src; }
      art.hidden = false;
    } else {
      art.hidden = true;
      art.dataset.src = "";
    }
    const frac = st.position && st.duration
      ? Math.min(1, st.position / st.duration) : 0;
    $("#np-bar").style.width = `${frac * 100}%`;
    $("#np-pos").textContent = fmtTime(st.position);
    $("#np-dur").textContent =
      st.position != null && st.duration == null ? "live" : fmtTime(st.duration);
    $("#btn-play").textContent = st.playing ? "⏸" : "▶";
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
$("#btn-stop").addEventListener("click", () => api("/stop", { method: "POST", body: {} }).then(pollStatus).catch((e) => toast(e.message)));

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
        toast(`Volumtak: ${r.volume}%`);
      } })
      .catch((e) => toast(e.message));
  }, 250);
});

document.querySelectorAll("input[name=output]").forEach((r) => {
  r.addEventListener("change", async () => {
    try {
      const res = await api("/output", { method: "POST", body: { device: r.value } });
      toast(res.spotify_restarted
        ? "Bytter lydutgang (Spotify starter på nytt …)" : "Lydutgang byttet");
    } catch (e) { toast(e.message); }
  });
});

/* --- library ------------------------------------------------------------- */

let LIB = { version: 1, sections: [] };

const ORDER_LABEL = {
  auto: "auto", newest_first: "nyeste først", oldest_first: "eldste først",
};

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
    wrap.innerHTML = "<div class='card'><p>Biblioteket er tomt — legg til den første lenka under.</p></div>";
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

  const play = document.createElement("button");
  play.textContent = "▶";
  play.title = "Spill nå";
  play.addEventListener("click", async () => {
    try {
      await api("/play", { method: "POST", body: { id: e.id } });
      toast(`Spiller: ${e.name}`);
    } catch (err) { toast(err.message); }
  });

  const del = document.createElement("button");
  del.textContent = "✕";
  del.title = "Fjern";
  del.className = "danger";
  del.addEventListener("click", async () => {
    if (!confirm(`Fjerne «${e.name}» fra biblioteket?`)) return;
    for (const s of LIB.sections) {
      s.entries = s.entries.filter((x) => x.id !== e.id);
    }
    LIB.sections = LIB.sections.filter((s) => s.entries.length);
    await saveLibrary();
    loadLibrary();
  });

  row.append(info, order, play, del);
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
    toast(`La til «${entry.name}»`);
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
  $("#set-cap").value = String(s.volume_cap);
  $("#set-idle").value = String(s.idle_shutdown_min);
}

for (const [id, key] of [["#set-screen", "screen_timeout_s"],
                         ["#set-cap", "volume_cap"],
                         ["#set-idle", "idle_shutdown_min"]]) {
  $(id).addEventListener("change", async () => {
    try {
      await api("/settings", { method: "PUT",
        body: { [key]: Number($(id).value) } });
      toast("Lagret");
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
  const rows = [];
  rows.push(["Batteri", sys.battery == null ? "ukjent"
    : `${Math.round(sys.battery)}%${sys.plugged ? " (lader)" : ""}`]);
  if (sys.disk) {
    rows.push(["SD-kort ledig", `${fmtBytes(sys.disk.free)} av ${fmtBytes(sys.disk.total)}`]);
  }
  for (const [k, v] of Object.entries(sys.caches || {})) {
    rows.push([k === "podcasts" ? "Podcast-cache" : "Spotify-cache", fmtBytes(v)]);
  }
  rows.push(["Wi-Fi", sys.wifi.enabled ? (sys.wifi.ssid || "på (ikke tilkoblet)") : "av"]);
  rows.push(["IP", sys.wifi.ip || "–"]);
  if (sys.cpu_temp != null) rows.push(["CPU-temp", `${sys.cpu_temp}°C`]);
  rows.push(["Boks", sys.hostname]);
  const dl = $("#sysinfo");
  dl.textContent = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  $("#btn-wifi").textContent = sys.wifi.enabled ? "Slå av Wi-Fi" : "Slå på Wi-Fi";
  $("#btn-wifi").dataset.enabled = sys.wifi.enabled ? "1" : "";
}

$("#btn-wifi").addEventListener("click", async () => {
  const enable = !$("#btn-wifi").dataset.enabled;
  if (!enable && !confirm(
    "Slå av Wi-Fi? Denne siden mister kontakten med boksen til Wi-Fi er på igjen (via skjermen eller omstart).")) return;
  try {
    await api("/system/wifi", { method: "POST", body: { enabled: enable } });
    toast(enable ? "Wi-Fi på" : "Wi-Fi av");
    loadSystem();
  } catch (e) { toast(e.message); }
});

$("#btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Slå av boksen?")) return;
  await api("/system/shutdown", { method: "POST", body: {} }).catch(() => {});
  toast("Slår av …", 10000);
});
$("#btn-restart").addEventListener("click", async () => {
  if (!confirm("Starte boksen på nytt?")) return;
  await api("/system/shutdown", { method: "POST", body: { restart: true } }).catch(() => {});
  toast("Starter på nytt …", 10000);
});

/* --- bluetooth ------------------------------------------------------------- */

async function loadBt() {
  let bt;
  try { bt = await api("/bt"); } catch (e) { return; }
  const active = bt.devices.find((d) => d.mac === bt.configured);
  $("#bt-current").textContent = bt.configured
    ? `Aktiv: ${active ? active.name : bt.configured}` +
      (active && active.connected ? " (tilkoblet)" : " (ikke tilkoblet nå)")
    : "Ingen høyttaler valgt ennå.";
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
    mac.textContent = d.mac + (d.paired ? " · paret" : "");
    info.append(name, mac);

    const use = document.createElement("button");
    use.textContent = d.connected ? "Tilkoblet" : "Koble til";
    use.disabled = d.connected;
    use.addEventListener("click", () => btAction("/bt/connect", { mac: d.mac },
      `Kobler til ${d.name} …`));

    const forget = document.createElement("button");
    forget.textContent = "Glem";
    forget.className = "danger";
    forget.addEventListener("click", () => {
      if (confirm(`Glemme «${d.name}»?`)) {
        btAction("/bt/forget", { mac: d.mac }, "Glemmer …");
      }
    });
    row.append(info, use, forget);
    wrap.appendChild(row);
  }
  $("#btn-pair").disabled = bt.pairing;
}

async function btAction(path, body, busyMsg) {
  toast(busyMsg, 60000);
  try {
    const r = await api(path, { method: "POST", body });
    toast(r.ok ? "OK" : (r.output || "Feilet").split("\n").pop(), r.ok ? 2500 : 8000);
  } catch (e) {
    toast(e.message, 6000);
  }
  loadBt();
}

$("#btn-pair").addEventListener("click", async () => {
  const btn = $("#btn-pair");
  btn.disabled = true;
  btn.textContent = "Parer …";
  await btAction("/bt/pair", {}, "Skanner og parer nærmeste høyttaler …");
  btn.disabled = false;
  btn.textContent = "Par nærmeste";
});

$("#btn-scan").addEventListener("click", async () => {
  const btn = $("#btn-scan");
  btn.disabled = true;
  btn.textContent = "Søker … (~25 s)";
  const wrap = $("#bt-found");
  wrap.textContent = "";
  try {
    const r = await api("/bt/scan", { method: "POST", body: {} });
    if (!r.found.length) {
      wrap.innerHTML = "<p class='dim'>Fant ingen nye enheter — er høyttaleren i paringsmodus og i nærheten?</p>";
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
      pick.textContent = "Par og koble til";
      pick.addEventListener("click", async () => {
        wrap.textContent = "";
        await btAction("/bt/connect", { mac: d.mac },
          `Parer og kobler til ${d.name} …`);
      });
      row.append(info, pick);
      wrap.appendChild(row);
    }
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Søk etter nye";
});

/* --- boot ------------------------------------------------------------------ */

pollStatus();
setInterval(pollStatus, 2000);
