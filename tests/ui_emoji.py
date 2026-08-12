#!/usr/bin/env python3
"""The screen's font cannot draw modern emoji, so ui.py scrubs the text
it receives from the API. Field 2026-08-11: a pumpkin and a crown in
podcast titles rendered as black .notdef boxes.

Font coverage below was MEASURED from the shipped DejaVuSans 2.37 cmap
(fontTools, 2026-08-12): the font has ♪♫★☆♥☀☃❄⚡✓▶♔, every arrow and
box-drawing glyph, and 64 of the 80 Emoticons faces — but only 12 of
the 768 Misc-Symbols-&-Pictographs (no 🎃👑🎵🔥) and none of Transport.
These pins encode that split; if the box ever ships a different font,
they are the place to re-measure."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = "/tmp/tapbox-ui-emoji"
os.makedirs(TMP, exist_ok=True)
for k in ("TAPBOX_RUN", "TAPBOX_STATE", "TAPBOX_CACHE"):
    os.environ[k] = TMP
os.environ["TAPBOX_UI_PNG"] = os.path.join(TMP, "screen.png")  # no SPI
# (the old TAPBOX_PNG name here was a no-op typo — QA review 2026-08-12)
os.environ["TAPBOX_EMOJI"] = "0"  # pins 1-9 pin the SCRUB pipeline; the
#                                   sprite pins below manage state directly
sys.path.insert(0, os.path.join(REPO, "pi"))
import ui  # noqa: E402

clean = ui.screen_text

# 1. the field bug: glyphs the font lacks are gone, the words survive
assert clean("🎃 Grøsserspesial") == "Grøsserspesial"
assert clean("👑 Kongen av bakgården") == "♔ Kongen av bakgården"
assert clean("Sommer 🏖 og sol") == "Sommer og sol"
print("1. pumpkin dropped, crown becomes ♔, text intact OK")

# 2. Norwegian text is never collateral damage — the whole point of
#    scrubbing by codepoint range rather than by "non-ascii"
for s in ("Bjørnen sover", "Æsj, sa Åse", "Fantorangens vitseshow",
          "Café — naïve piñata", "Delfi & Dolfy (1:13)", "100 % moro!"):
    assert clean(s) == s, s
print("2. æøå, accents, punctuation and digits pass through OK")

# 3. symbols DejaVu DOES have must not be touched
for s in ("♪ Vuggesang", "★ Favoritt", "☀ Sommer", "♥ Mamma",
          "✓ Ferdig", "▶ Spill", "☃ Vinter", "⚡ Torden"):
    assert clean(s) == s, s
print("3. font-native symbols survive untouched OK")

# 4. emoji with a good twin are translated, not dropped: a title that
#    is ONLY an emoji must not become empty
assert clean("🎵 Barnesanger") == "♪ Barnesanger"
assert clean("🎧") == "♪"
assert clean("⭐✨ Ukens beste") == "★★ Ukens beste"
assert clean("💜 Godnatt") == "♥ Godnatt"
assert clean("🔥 Nyhet") == "▲ Nyhet"
print("4. mapped emoji keep a visible stand-in OK")

# 5. the Emoticons split: the 64 faces DejaVu draws stay as they are,
#    the 16 it lacks fall back to their nearest neighbour
assert clean("😴 Godnatt") == "😴 Godnatt"
assert clean("😂😍😡") == "😂😍😡"
assert clean("🙂") == "😊"
assert clean("🙏 Takk") == "Takk"        # gesture people: no twin, dropped
assert clean("🤣") == "😂"
print("5. covered faces kept, missing faces mapped or dropped OK")

# 6. the invisibles: a variation selector left behind after its base
#    char was mapped would draw its own box
assert clean("☀️ Sol") == "☀ Sol"       # ☀️ (base + VS16)
assert clean("\U0001F468‍\U0001F469 Familie") == "Familie"  # ZWJ
assert clean("\U0001F44D\U0001F3FD Bra") == "Bra"   # skin-tone modifier
assert clean("\U0001F1F3\U0001F1F4 Norge") == "Norge"  # 🇳🇴 flag
print("6. variation selectors, ZWJ, skin tones and flags removed OK")

# 7. no double spaces or ragged edges left where an emoji used to be
assert clean("Jul 🎄 2026") == "Jul 2026"
assert clean("🎃  🎃  Halloween") == "Halloween"
assert clean("   🎃 Kant   ") == "Kant"
print("7. whitespace collapses cleanly around removals OK")

# 8. the payload walk: only strings change, and nothing else in the
#    API answer may be rewritten — ids, urls and numbers are load-bearing
payload = {"title": "🎃 Grøss", "position": 61.5, "playing": True,
           "id": "spotify:track:5fbQ", "artwork": None,
           "url": "https://feeds.acast.com/public/shows/5fc2?x=1",
           "queue": [{"name": "🎵 En", "n": 1}, {"name": "To", "n": 2}]}
out = ui._screen_safe(payload)
assert out["title"] == "Grøss"
assert out["queue"][0]["name"] == "♪ En"
assert out["position"] == 61.5 and out["playing"] is True
assert out["id"] == payload["id"] and out["url"] == payload["url"]
assert out["artwork"] is None
assert payload["title"] == "🎃 Grøss", "the original payload was mutated"
print("8. nested payload scrubbed, non-strings and ids untouched OK")

# 9. cheap enough to run on every 1s status poll
import time  # noqa: E402
big = {"tracks": [{"title": f"🎵 Spor {i} — Bjørnen sover ★", "n": i}
                  for i in range(200)]}
t0 = time.monotonic()
for _ in range(10):
    ui._screen_safe(big)
per = (time.monotonic() - t0) / 10
assert per < 0.05, f"too slow: {per*1000:.1f}ms for 200 rows"
print(f"9. 200-row payload scrubbed in {per*1000:.2f}ms OK")


# --- the sprite path (design review 2026-08-12) ----------------------------
# Everything below runs against RichDraw. On a box with the real Noto
# CBDT font the sprites are color emoji; here any TTF will do for the
# GEOMETRY pins (the glyph may be a tofu box — we pin layout, caching
# and fallback, not beauty). Color itself is pinned only when the real
# font is present (the Pi), skipped elsewhere.
from PIL import Image, ImageDraw, ImageFont  # noqa: E402


def frame(fn, w=240, h=64):
    img = Image.new("RGB", (w, h), (18, 18, 24))
    fn(img)
    return img


def draw_ref(img, s, a=None, f=None):
    ImageDraw.Draw(img).text((120, 20), s, font=f or ui.F_MED,
                             fill=(220, 220, 220), anchor=a)


def draw_rich(img, s, a=None, f=None):
    ui._draw(img).text((120, 20), s, font=f or ui.F_MED,
                       fill=(220, 220, 220), anchor=a)


# 10. P1 — pixel identity: emoji-free frames byte-equal across every
#     anchor and font ui.py actually uses. This pin forces the
#     guard-first fast path (1px anchor drift was measured on 90/160
#     centered draws when x is converted naively).
CORPUS = ["Bjørnen sover", "A: play on box speaker", "Æsj, sa Åse!",
          "♪ Vuggesang", "★ Favoritt — ☀ Sommer", "Delfi & Dolfy (1:13)",
          "Speaker disconnected"]
assert ui.emoji_active() is False  # env kill switch -> scrub pipeline
for s in CORPUS:
    for a in (None, "ma", "mm", "ra"):
        for f in (ui.F_BIG, ui.F_MED, ui.F_SMALL):
            ref = frame(lambda im: draw_ref(im, s, a, f)).tobytes()
            got = frame(lambda im: draw_rich(im, s, a, f)).tobytes()
            assert ref == got, (s, a)
print("10. RichDraw is byte-identical for emoji-free text OK")

# 11. P2 — sprites off => yesterday's pipeline exactly: the RichDraw
#     frame equals a plain draw of the scrubbed string, original anchor
for s in ("🎃 Grøsserspesial", "👑 Kongen", "🎵 Barnesanger", "☀️ Sol"):
    for a in (None, "ma", "ra"):
        ref = frame(
            lambda im: draw_ref(im, ui.screen_text(s), a)).tobytes()
        got = frame(lambda im: draw_rich(im, s, a)).tobytes()
        assert ref == got, (s, a)
print("11. no-font path renders the shipped scrub, byte-for-byte OK")

# 12. P3 — the tofu guard: a font that OPENS but cannot draw emoji
#     (DejaVu, FontAwesome) must fail the self-test — a .notdef box has
#     a perfectly good bbox, which is how the 2026-08-11 field bug
#     would re-enter through the cache as black-box PNGs.
TTF = next((p for p in (
    os.environ.get("TAPBOX_TEST_TTF", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/doc/pipx/html-docs/fonts/fontawesome-webfont.ttf",
) if p and os.path.exists(p)), None)
os.environ["TAPBOX_EMOJI"] = "1"
if TTF:
    ui._EMOJI_FONT_PATHS = (TTF,)
    ui._EMOJI_STRIKES = (64,)
    ui._emoji.update(state=None, font=None)
    assert ui.emoji_active() is False, "tofu font must be rejected"
    assert not (os.path.isdir(ui.UI_EMOJI_DIR)
                and os.listdir(ui.UI_EMOJI_DIR)), \
        "rejected font wrote cache files"
    print("12. tofu font rejected by the self-test, cache untouched OK")
else:
    print("12. SKIP (no TTF on this machine to probe with)")

# The remaining pins need an ACTIVE sprite state; inject one directly
# (any TTF renders SOME box for the cluster — geometry is what we pin).
if TTF:
    f64 = ImageFont.truetype(TTF, 64)
    _asc, _desc = f64.getmetrics()
    ui._emoji.update(state="on", font=f64, asc=_asc, em_h=_asc + _desc)
    ui._sprites.clear()

    # 13. P4 — textlength equals painted ink (±3px bearing slack):
    #     wrap/underline decisions must never overflow the 240px panel.
    img = Image.new("RGB", (240, 40), (0, 0, 0))
    d = ui._draw(img)
    s = "🎃 Grøss"
    tl = d.textlength(s, font=ui.F_MED)
    d.text((0, 0), s, font=ui.F_MED, fill=(255, 255, 255))
    ink = img.getbbox()
    assert ink is not None and tl > 0
    assert abs(ink[2] - tl) <= 3, (ink, tl)
    print("13. textlength matches the pixels actually painted OK")

    # 14. P5 — declines stay scrubbed even when sprites are live: ZWJ
    #     families, flags, keycaps and sliced-off invisibles render
    #     exactly as the scrub pipeline would.
    for s, want in (("👨‍👩‍👧 Familie", "Familie"),
                    ("🇳🇴 Norge", "Norge"),
                    ("1️⃣ En", "1 En"),
                    ("‍ Hei", "Hei"),           # lone ZWJ off a slice
                    ("\U0001f1f3 rest", "rest")):    # lone flag half
        ref = frame(lambda im: draw_ref(im, want, "ma")).tobytes()
        got = frame(lambda im: draw_rich(im, s, "ma")).tobytes()
        assert ref == got, s
    print("14. ZWJ/flag/keycap and sliced invisibles stay scrubbed OK")

    # 15. P6 — the cache: built once, atomic on disk, self-healing on
    #     corruption, and the library sweeper leaves the dir alone.
    ui._sprites.clear()
    lh = sum(ui.F_MED.getmetrics())
    spr = ui.emoji_sprite("🎃", lh)
    assert spr is not None and spr.size[1] == lh
    files = os.listdir(ui.UI_EMOJI_DIR)
    assert files and not [x for x in files if x.endswith(".part")]
    path = os.path.join(ui.UI_EMOJI_DIR, files[0])
    with open(path, "wb") as fh:
        fh.write(b"junk")               # corrupt it
    ui._sprites.clear()
    spr2 = ui.emoji_sprite("🎃", lh)
    assert spr2 is not None, "corrupt sprite must be re-rendered"
    with open(path, "rb") as fh:
        assert fh.read(4) == b"\x89PNG", "corrupt file was not healed"
    from tapbox import content  # noqa: E402
    content.prune_cache([])
    assert os.path.isdir(ui.UI_EMOJI_DIR) and os.listdir(ui.UI_EMOJI_DIR), \
        "prune_cache wiped the sprite dir (content.py allow-list)"
    print("15. sprite cache atomic, self-healing, sweeper-proof OK")

    # 16. marquee charges a live cluster 2 chars, so a sprite title
    #     scrolls instead of overrunning its row; plain text unchanged
    txt = "🎃" + "x" * 23                # 24 chars, 25 with the charge
    _win, rolls = ui.marquee(txt, 24)
    assert rolls is True
    _win, rolls = ui.marquee("x" * 24, 24)
    assert rolls is False
    print("16. marquee charges sprite clusters 2 chars OK")

    # 17. color, only where the REAL font lives (the Pi): the pumpkin
    #     frame must contain orange-dominant pixels.
    NOTO = ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf")
    if any(os.path.exists(p) for p in NOTO):
        ui._EMOJI_FONT_PATHS = NOTO
        ui._EMOJI_STRIKES = (109, 128, 136, 160)
        ui._emoji.update(state=None, font=None)
        ui._sprites.clear()
        assert ui.emoji_active() is True, "real Noto failed the self-test"
        img = frame(lambda im: draw_rich(im, "🎃 Grøsserspesial", "ma"))
        orange = sum(1 for r, g, b in img.getdata()
                     if r > 150 and g > 40 and b < 100 and r > b + 80)
        assert orange > 20, f"no orange pumpkin pixels ({orange})"
        print("17. real Noto renders an orange pumpkin OK")
    else:
        print("17. SKIP (no Noto Color Emoji here — rig-only pin)")

    ui._emoji.update(state="off", font=None)  # leave a clean state

print("\nUI EMOJI OK — the screen loses only what it could not draw, "
      "and draws what it can in color.")
