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
os.environ["TAPBOX_PNG"] = os.path.join(TMP, "screen.png")  # no SPI here
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

print("\nUI EMOJI OK — the screen loses only what it could not draw.")
