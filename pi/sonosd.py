#!/usr/bin/env python3
"""tapbox-sonos — the Sonos sidecar (UPnP via SoCo, venv python).

tapboxd is stdlib-only on system python; SoCo lives in /opt/tapbox/venv.
This process bridges the two: a small JSON API on 127.0.0.1 that tapboxd
drives, same shape as the tapboxd <-> go-librespot split.

Design (three-agent review 2026-08-08/09):
- THE POLLER LIVES HERE. /state is a memory read of the last snapshot —
  never a live SOAP call. Stall/takeover detection needs a SEQUENCE of
  samples (a cached sample re-served must be tellable from a fresh one),
  hence the monotonic `seq`. tapboxd's /status must never wait on a
  sleeping speaker over PS-throttled wifi.
- ONE session, not per-uid: the box is a single sequencer and renderer.
  /play means "become the session"; other verbs take an optional if_uid
  and 409 on mismatch, closing the stale-command race.
- /adopt re-attaches to a session already playing (daemon or sidecar
  restarted) WITHOUT issuing transport commands — /play would restart
  the episode over music that never stopped.
- Success/error shapes are a closed set (tests/sonos_contract.py). An
  unknown condition must land in the conservative branch on the tapboxd
  side, so unknown things are never invented here.
- uid -> ip persists in STATE_DIR/sonos.json: SSDP costs seconds and
  multicast-over-wifi drops; direct SoCo(ip) is the owner's own proven
  path (sonos-remotes). Discovery runs on cache miss, explicit rescan,
  or a failed play — never on a timer.
- soco imports LAZILY on first use: always-on process, ~20 MB deferred
  until Sonos is actually used (architect lifecycle review).

Content kinds (v1):
  url                plain http(s) audio the speaker fetches from origin
  nrk_program        NRK series via the NRK Radio Sonos service
                     (x-sonos-http, sid=277) — the owner's sonos-remotes
                     recipe, plus the <desc> element his version lacked
  spotify_sharelink  SoCo ShareLink; SONOS OWNS THE QUEUE for spotify
                     (owner decision 2026-08-09) — next/prev go to the
                     speaker's queue, not our sequencer

Never carried over from sonos-remotes: 0.0.0.0 binds, default secrets,
CIDR auth bypasses. This binds 127.0.0.1 only.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

from tapbox.paths import STATE_DIR  # noqa: E402

PORT = int(os.environ.get("TAPBOX_SONOS_PORT", "3681"))
CACHE_FILE = os.path.join(STATE_DIR, "sonos.json")
POLL_S = float(os.environ.get("TAPBOX_SONOS_POLL", "5"))
POLL_PAUSED_S = float(os.environ.get("TAPBOX_SONOS_POLL_PAUSED", "15"))
# NRK Radio's Sonos service id; its service type = sid*256 + 7
NRK_SID = 277
NRK_SVCTYPE = NRK_SID * 256 + 7
STALL_POLLS = 3   # PLAYING + frozen RelTime this many polls -> one retry


def log(msg):
    print(f"sonosd: {msg}", flush=True)


def _soco():
    """Lazy import — pay the ~20 MB only when Sonos is actually used."""
    import soco  # noqa: F401  (venv-only dependency)
    return soco


# --- speaker cache ---------------------------------------------------------

_cache_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
            return d if isinstance(d.get("players"), dict) else {"players": {}}
    except (OSError, ValueError):
        return {"players": {}}


def _save_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def rescan():
    """SSDP + zone topology via SoCo. Merges into the cache — a scan that
    misses a speaker (wifi multicast drops) must never delete the row the
    kid was aiming at; a play to a dead one fails cleanly instead."""
    soco = _soco()
    found = soco.discover(timeout=3) or set()
    with _cache_lock:
        cache = _load_cache()
        for z in found:
            try:
                if not z.is_visible:
                    continue  # bonded surrounds/subs — not pickable rooms
                cache["players"][z.uid] = {
                    "ip": z.ip_address, "name": z.player_name,
                    "seen_at": time.time()}
            except Exception as e:
                log(f"scan: skipping a zone ({e.__class__.__name__})")
        cache["fetched_at"] = time.time()
        _save_cache(cache)
        return cache


def players():
    with _cache_lock:
        return _load_cache()


def _speaker(uid):
    """SoCo instance for a cached uid — direct IP, no discovery. Verifies
    the uid still matches (DHCP moves IPs); one mismatch triggers rescan."""
    soco = _soco()
    with _cache_lock:
        rec = _load_cache()["players"].get(uid)
    if rec is None:
        raise KeyError(uid)
    s = soco.SoCo(rec["ip"])
    try:
        if s.uid != uid:
            raise KeyError(uid)  # IP moved under the cache
    except KeyError:
        raise
    except Exception:
        # unreachable — let the caller classify; do not rescan on the
        # poll path (that is the play path's job)
        pass
    return s


# --- DIDL ------------------------------------------------------------------

DIDL_NS = ('xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
           'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"')


def _hms(sec):
    sec = max(0, int(sec or 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _hms_to_s(s):
    try:
        h, m, sec = (s or "0:00:00").split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, AttributeError):
        return None


MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav",
        ".ogg": "audio/ogg", ".opus": "audio/ogg"}


def _mime_for(uri):
    path = urllib.parse.urlparse(uri).path.lower()
    for ext, mime in MIME.items():
        if path.endswith(ext):
            return mime
    return "audio/mpeg"


def didl(uri, title, artist=None, album=None, art=None, duration_s=None,
         upnp_class="object.item.audioItem.musicTrack",
         protocol=None, desc=None):
    """One DIDL-Lite item. escape() is applied HERE to each value exactly
    once; SoCo escapes the whole string again when it inlines it as the
    SOAP argument. Two levels, once each — dropping either is the classic
    'audio plays, metadata blank' (implementer review 2026-08-08; the
    &amp;amp; you will see on the wire is CORRECT)."""
    proto = protocol or f"http-get:*:{_mime_for(uri)}:*"
    dur = f' duration="{_hms(duration_s)}"' if duration_s else ""
    tags = [f"<dc:title>{escape(title or 'TapBox')}</dc:title>"]
    if artist:
        # both forms: different Sonos controller versions read different
        # ones — emitting both ends the guessing
        tags.append(f"<dc:creator>{escape(artist)}</dc:creator>")
        tags.append(f"<upnp:artist>{escape(artist)}</upnp:artist>")
    if album:
        tags.append(f"<upnp:album>{escape(album)}</upnp:album>")
    if art:
        # must be a LAN IP url — Sonos does not resolve mDNS (.local)
        tags.append(f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>")
    tags.append(f"<upnp:class>{escape(upnp_class)}</upnp:class>")
    if desc:
        # service items (x-sonos-http) need the cdudn descriptor for the
        # controller app to resolve service metadata — the element the
        # owner's sonos-remotes lacked, and the prime suspect for its
        # historical partial-metadata sore point
        tags.append('<desc id="cdudn" nameSpace='
                    '"urn:schemas-rinconnetworks-com:metadata-1-0/">'
                    f"{escape(desc)}</desc>")
    tags.append(f'<res protocolInfo="{proto}"{dur}>{escape(uri)}</res>')
    return (f"<DIDL-Lite {DIDL_NS}>"
            '<item id="tapbox-1" parentID="-1" restricted="true">'
            + "".join(tags) + "</item></DIDL-Lite>")


# --- NRK Radio service recipe (ported from palchrb/sonos-remotes) ----------

_sn_cache = {}  # speaker ip -> account serial for sid=277


def _nrk_serial(ip):
    """The household's account serial for the NRK Radio service. The
    owner's original hardcoded sn=14 breaks silently if the service is
    ever re-linked, so it is looked up from the speaker instead."""
    if ip in _sn_cache:
        return _sn_cache[ip]
    import xml.etree.ElementTree as ET
    try:
        with urllib.request.urlopen(
                f"http://{ip}:1400/status/accounts", timeout=5) as r:
            root = ET.fromstring(r.read())
        for acct in root.iter("Account"):
            if acct.get("Type") == str(NRK_SVCTYPE):
                sn = acct.get("SerialNum")
                if sn is not None:
                    _sn_cache[ip] = sn
                    return sn
    except Exception as e:
        log(f"nrk serial lookup failed ({e.__class__.__name__}) — "
            "falling back to sn=14")
    return "14"  # the owner's known-working household value


def nrk_program_uri(ip, series, program_id):
    sn = _nrk_serial(ip)
    return (f"x-sonos-http:series%3a{urllib.parse.quote(series)}"
            f"%3a1%3a{program_id}.unknown?sid={NRK_SID}&flags=0&sn={sn}")


def nrk_desc():
    return f"SA_RINCON{NRK_SVCTYPE}_X_#Svc{NRK_SVCTYPE}-0-Token"


# --- the single session ----------------------------------------------------

class Session:
    """Exactly one; guarded by its own lock so the stall retry's four-call
    sequence is atomic against a concurrent /play."""

    def __init__(self):
        self.lock = threading.Lock()
        self.armed = False
        self.uid = None
        self.kind = None
        self.uri = None          # what WE set (ours-detection)
        self.snapshot = {"armed": False, "seq": 0, "stale_s": None}
        self._seq = 0
        self._frozen = 0         # consecutive PLAYING polls with same pos
        self._last_pos = None
        self._retried_at = None
        self._last_ok = None     # monotonic of last successful poll
        self._wake = threading.Event()

    # -- snapshot plumbing --

    def publish(self, **fields):
        self._seq += 1
        snap = {"armed": self.armed, "uid": self.uid, "kind": self.kind,
                "seq": self._seq, "retried_at": self._retried_at}
        snap.update(fields)
        self.snapshot = snap

    def state(self):
        snap = dict(self.snapshot)
        # stale_s computed HERE from monotonic — an age, never a wall
        # timestamp (the box's RTC jumps at boot; ages cannot)
        snap["stale_s"] = (None if self._last_ok is None
                           else round(time.monotonic() - self._last_ok, 1))
        return snap

    # -- transport verbs (called with self.lock held) --

    def _spk(self):
        return _speaker(self.uid)

    def play(self, body):
        soco = _soco()
        uid = body["uid"]
        kind = body.get("kind", "url")
        self.uid, self.kind = uid, kind
        spk = self._spk()
        start = float(body.get("start_s") or 0)
        built = None
        if kind == "url":
            uri = body["uri"]
            try:
                # the transport plays this uri DIRECTLY — the queue is
                # not involved. Clear it anyway: a leftover spotify queue
                # in the Sonos app next to a playing podcast read as "the
                # queue is wrong" (field 2026-08-09, cosmetic)
                spk.clear_queue()
            except Exception:
                pass
            built = didl(uri, body.get("title"), body.get("artist"),
                         body.get("album"), body.get("art"),
                         body.get("duration_s"))
            spk.avTransport.SetAVTransportURI([
                ("InstanceID", 0), ("CurrentURI", uri),
                ("CurrentURIMetaData", built)])
            spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
        elif kind == "nrk_program":
            uri = nrk_program_uri(spk.ip_address, body["series"],
                                  body["program_id"])
            built = didl(uri, body.get("title"), body.get("artist"),
                         body.get("album"), body.get("art"),
                         body.get("duration_s"),
                         upnp_class="object.item.audioItem.show",
                         protocol="sonos.com-http:*:audio/mpeg:*",
                         desc=nrk_desc())
            spk.avTransport.SetAVTransportURI([
                ("InstanceID", 0), ("CurrentURI", uri),
                ("CurrentURIMetaData", built)])
            spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
        elif kind == "spotify_sharelink":
            from soco.plugins.sharelink import ShareLinkPlugin
            # NORMAL kills family shuffle/repeat leftovers AND makes the
            # queue's play order equal its index order — the legality of
            # every positional jump rests on this (architect Q2).
            try:
                spk.play_mode = "NORMAL"
            except Exception:
                pass
            spk.clear_queue()
            r = ShareLinkPlugin(spk).add_share_link_to_queue(body["uri"])
            # FirstTrackNumberEnqueued: never assume the queue starts at
            # 1, even after clear_queue (architect Q1 outbound)
            try:
                self.q_base = int(r) if r else 1
            except (TypeError, ValueError):
                self.q_base = 1
            try:
                self.q_len = int(spk.queue_size)
            except Exception:
                self.q_len = None
            idx = int(body.get("track_index") or 0)
            spk.play_from_queue(self.q_base - 1 + idx)
            uri = body["uri"]
        else:
            raise ValueError(f"unknown kind: {kind}")
        self.uri = uri
        self.armed = True
        self._didl_checked = False
        self._frozen, self._last_pos, self._retried_at = 0, None, None
        sought = self._seek_settled(spk, start) if start >= 5 else True
        self._wake.set()
        return {"ok": True, "uid": uid, "uri": uri,
                "sought": bool(sought), "didl": built,
                "base": getattr(self, "q_base", None),
                "queue_len": getattr(self, "q_len", None),
                "play_mode": "NORMAL" if kind == "spotify_sharelink"
                else None}

    def _seek_settled(self, spk, start_s, timeout=8):
        """SetURI -> Play -> wait PLAYING -> Seek. Against a STOPPED
        transport Seek is UPnP 701 (nothing to seek in yet); costs ~1s
        of audio from 0:00. sought=false is a DEGRADE, not an error."""
        soco = _soco()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = spk.avTransport.GetTransportInfo([("InstanceID", 0)])
                if info.get("CurrentTransportState") == "PLAYING":
                    break
            except Exception:
                pass
            time.sleep(0.3)
        for _ in range(3):
            try:
                spk.avTransport.Seek([("InstanceID", 0),
                                      ("Unit", "REL_TIME"),
                                      ("Target", _hms(start_s))])
                return True
            except soco.exceptions.SoCoUPnPException as e:
                if str(getattr(e, "error_code", "")) not in ("701", "710",
                                                             "711"):
                    return False
                time.sleep(0.4)
            except Exception:
                return False
        log(f"seek to {_hms(start_s)} refused — playing from the top")
        return False

    def adopt(self, body):
        """Re-attach after a restart on either side: session bookkeeping
        only, ZERO transport commands — /play here would restart the
        episode over music that never stopped."""
        self.uid = body["uid"]
        self.kind = body.get("kind")
        self.uri = body.get("uri")
        self.armed = True
        self._frozen, self._last_pos, self._retried_at = 0, None, None
        self._wake.set()
        return {"ok": True, "uid": self.uid}

    def verb(self, name, body):
        want = body.get("if_uid")
        if want and want != self.uid:
            return None  # 409 at the HTTP layer
        spk = self._spk()
        if name == "pause":
            spk.avTransport.Pause([("InstanceID", 0)])
            self._wake.set()  # re-poll NOW: the cached snapshot still
            # says PLAYING, and the paused cadence is 15s (QA 2026-08-09)
        elif name == "resume":
            spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
            self._wake.set()
        elif name == "stop":
            spk.avTransport.Stop([("InstanceID", 0)])
            self.armed = False
            self._wake.set()
        elif name == "seek":
            spk.avTransport.Seek([("InstanceID", 0), ("Unit", "REL_TIME"),
                                  ("Target", _hms(float(body["s"])))])
        elif name == "volume":
            spk.volume = max(0, min(100, int(body["v"])))
        elif name == "queue_play":
            # tapbox owns the logic for EVERY kind now (v2) — this jumps
            # the speaker's queue to an absolute 0-based position and
            # optionally seeks. The old delegated next/prev died with v1.
            pos = int(body["index"])
            base = getattr(self, "q_base", 1) or 1
            qlen = getattr(self, "q_len", None)
            absidx = base - 1 + pos
            if qlen is not None:
                absidx = max(base - 1, min(absidx, base - 2 + qlen))
            spk.play_from_queue(absidx)
            start = float(body.get("start_s") or 0)
            sought = self._seek_settled(spk, start) if start >= 5 else True
            self._wake.set()
            return {"ok": True, "sought": bool(sought)}
        return {"ok": True}

    # -- the poller --

    def _classify(self, spk):
        """One poll -> the fields tapboxd's policy dispatch needs. One
        GetPositionInfo + one GetTransportInfo; volume piggybacks."""
        pos = spk.avTransport.GetPositionInfo([("InstanceID", 0)])
        tr = spk.avTransport.GetTransportInfo([("InstanceID", 0)])
        transport = tr.get("CurrentTransportState") or "STOPPED"
        if transport == "TRANSITIONING":
            # buffering/track-change limbo: report the last REAL state —
            # downstream treats TRANSITIONING as not-playing, which
            # painted a pause icon during every fresh start (G1-d)
            transport = getattr(self, "_last_tr", None) or "PLAYING"
        else:
            self._last_tr = transport
        track_uri = pos.get("TrackURI") or ""
        rel = _hms_to_s(pos.get("RelTime"))
        dur = _hms_to_s(pos.get("TrackDuration"))
        def _norm(u):
            # the speaker re-encodes urls (percent-escaping, and some
            # firmwares swap the scheme prefix) — an exact match called
            # OUR OWN nrk episode foreign, which killed playing/progress
            # for every url-kind card (field 2026-08-09)
            u = urllib.parse.unquote(u or "")
            for p in ("x-rincon-mp3radio://", "aac://", "https://",
                      "http://"):
                if u.startswith(p):
                    return u[len(p):]
            return u
        ours = bool(self.uri) and (
            _norm(track_uri) == _norm(self.uri)
            or self.kind == "spotify_sharelink" and track_uri.startswith(
                ("x-sonos-spotify:", "x-sonosprog-spotify:")))
        if not ours and self.uri and track_uri                 and self.kind != "spotify_sharelink":
            if getattr(self, "_ours_logged", None) != track_uri:
                self._ours_logged = track_uri
                log(f"not-ours? speaker={track_uri!r} vs set={self.uri!r}")
        grouped_away = False
        coordinator = None
        try:
            g = spk.group
            if g is not None and g.coordinator is not None \
                    and g.coordinator.uid != spk.uid:
                grouped_away = True
                coordinator = g.coordinator.uid
        except Exception:
            pass
        lost = (transport == "STOPPED" and not track_uri
                and self.kind != "spotify_sharelink")
        fields = {
            "reachable": True, "transport": transport,
            "rel_s": rel, "dur_s": dur, "uri": track_uri, "ours": ours,
            "foreign_uri": None if ours else (track_uri or None),
            "grouped_away": grouped_away, "coordinator": coordinator,
            "lost_session": lost, "volume": spk.volume,
        }
        # track metadata for the sharelink path: the box's screen shows
        # what the SONOS queue is on, since sonos owns that queue
        if self.kind == "spotify_sharelink":
            fields["track_no"] = None
            try:
                fields["track_no"] = int(pos.get("Track"))
            except (TypeError, ValueError):
                pass
            fields["base"] = getattr(self, "q_base", None)
            fields["queue_len"] = getattr(self, "q_len", None)
            # the playing track's OWN uri, percent-decoded — exact under
            # every queue divergence, zero extra SOAP (architect Q1)
            if track_uri.startswith(("x-sonos-spotify:",
                                     "x-sonosprog-spotify:")):
                raw = track_uri.split(":", 1)[1].split("?")[0]
                fields["track_spotify_uri"] = urllib.parse.unquote(raw)
        if ours and self.kind in ("url", "nrk_program") \
                and not getattr(self, "_didl_checked", False):
            self._didl_checked = True
            m = pos.get("TrackMetaData") or ""
            if not m or m == "NOT_IMPLEMENTED":
                log("didl REJECTED by the speaker (no TrackMetaData) — "
                    "metadata will not render in the Sonos app")
            else:
                log(f"didl accepted ({len(m)} bytes echoed)")
        if ours and self.kind == "spotify_sharelink":
            meta = pos.get("TrackMetaData") or ""
            fields["track_title"] = _didl_field(meta, "title")
            fields["track_artist"] = _didl_field(meta, "creator")
            # album art: Sonos hands back a RELATIVE /getaa?... url that
            # the speaker itself serves — absolutize it so the box's
            # screen (and PWA) can fetch it directly
            art = _didl_field(meta, "albumArtURI", ns="upnp")
            if art and art.startswith("/"):
                art = f"http://{spk.ip_address}:1400{art}"
            fields["track_art"] = art
        return fields

    def poll_loop(self):
        while True:
            if not self.armed:
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            with self.lock:
                if not self.armed:
                    continue
                try:
                    fields = self._classify(self._spk())
                    self._last_ok = time.monotonic()
                    self._stall_bookkeeping(fields)
                    self.publish(**fields)
                except Exception as e:
                    # speaker unreachable — sidecar-up-speaker-down is its
                    # own shape, distinct from ECONNREFUSED (sidecar down)
                    self.publish(reachable=False, transport="UNREACHABLE",
                                 error=e.__class__.__name__)
            snap = self.snapshot
            wait = (POLL_PAUSED_S if snap.get("transport")
                    == "PAUSED_PLAYBACK" else POLL_S)
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def _stall_bookkeeping(self, fields):
        """PLAYING + our URI + frozen RelTime across STALL_POLLS -> ONE
        retry (Stop/SetURI/Seek/Play equivalent), then hands off. N whole
        polls, not one: a wifi rebuffer false-positives (the same lesson
        as the mpv watchdog's FREEZE_ESCALATE)."""
        if fields["transport"] != "PLAYING" or not fields["ours"] \
                or self.kind == "spotify_sharelink":
            self._frozen, self._last_pos = 0, None
            return
        rel = fields["rel_s"]
        if rel is not None and rel == self._last_pos:
            self._frozen += 1
        else:
            self._frozen = 0
        self._last_pos = rel
        if self._frozen >= STALL_POLLS and self._retried_at is None:
            self._retried_at = time.time()
            log("position frozen — one in-place retry")
            try:
                spk = self._spk()
                spk.avTransport.Stop([("InstanceID", 0)])
                spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
                if rel and rel >= 5:
                    self._seek_settled(spk, rel)
            except Exception as e:
                log(f"stall retry failed ({e.__class__.__name__})")


SESSION = Session()


def _didl_field(meta_xml, tag, ns="dc"):
    """Pull one text field out of a TrackMetaData DIDL without namespace
    gymnastics — display only, never used for control decisions."""
    import re as _re
    m = _re.search(rf"<{ns}:{tag}[^>]*>([^<]*)</{ns}:{tag}>",
                   meta_xml or "")
    import html as _html
    return _html.unescape(m.group(1)) if m else None


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except OSError:
            # client gave up waiting (mash of controls) — the work was
            # done; a BrokenPipe traceback per press is just journal spam
            pass

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self._send(200, {"ok": True})
        elif u.path == "/state":
            q = urllib.parse.parse_qs(u.query)
            if q.get("live", ["0"])[0] == "1" and SESSION.armed:
                # switch-back wants the EXACT second: one live probe with
                # a hard budget, falling back to the last snapshot — the
                # caller never waits on a sleeping speaker (owner ask
                # 2026-08-09)
                done = threading.Event()

                def probe():
                    try:
                        with SESSION.lock:
                            f = SESSION._classify(SESSION._spk())
                            SESSION._last_ok = time.monotonic()
                            SESSION.publish(**f)
                    except Exception:
                        pass
                    done.set()

                threading.Thread(target=probe, daemon=True).start()
                done.wait(timeout=1.5)
            self._send(200, SESSION.state())
        elif u.path == "/players":
            q = urllib.parse.parse_qs(u.query)
            try:
                cache = (rescan() if q.get("rescan", ["0"])[0] == "1"
                         else players())
            except Exception as e:
                self._send(502, {"error": "scan-failed",
                                 "detail": e.__class__.__name__})
                return
            # uid + name only over GET: the speaker IPs are LAN topology
            # and every GET is token-free by the box's SAFE rule
            self._send(200, {"players": [
                {"uid": uid, "name": rec.get("name")}
                for uid, rec in sorted(cache["players"].items(),
                                       key=lambda kv: kv[1].get("name") or "")
            ]})
        else:
            self._send(404, {"error": "not-found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"error": "bad-json"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/play":
                with SESSION.lock:
                    self._send(200, SESSION.play(body))
            elif path == "/adopt":
                with SESSION.lock:
                    self._send(200, SESSION.adopt(body))
            elif path in ("/pause", "/resume", "/stop", "/seek", "/volume",
                          "/queue_play"):
                with SESSION.lock:
                    r = SESSION.verb(path[1:], body)
                if r is None:
                    self._send(409, {"error": "uid-mismatch",
                                     "uid": SESSION.uid})
                else:
                    self._send(200, r)
            else:
                self._send(404, {"error": "not-found"})
        except KeyError as e:
            self._send(404, {"error": "unknown-uid", "detail": str(e)})
        except ValueError as e:
            self._send(400, {"error": "bad-request", "detail": str(e)})
        except Exception as e:
            # a SoCoUPnPException lands here too: the speaker said no.
            # 502 = "speaker problem", never a crash — tapboxd's policy
            # maps it to the conservative branch.
            self._send(502, {"error": "speaker",
                             "detail": f"{e.__class__.__name__}: {e}"})


def main():
    threading.Thread(target=SESSION.poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    assert srv.server_address[0] == "127.0.0.1"  # never a LAN bind
    log(f"up on 127.0.0.1:{PORT} (soco loads on first use)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
