#!/usr/bin/env python3
"""Render the PWA icons from the artwork, so they cannot drift from it.

    python3 pi/art/make-icons.py            # rebuild web/icon-*.png
    python3 pi/art/make-icons.py --rounded  # from the rounded artboard

The mark is a set of hand-shaped rings — the radius wanders ~4% around
the turn — so they are drawn as the polylines they are in the SVG, not
approximated with ellipses. Same reasoning, and the same parser, as the
boot splash in ui.py.

SHARP is the default and the right choice for app icons: iOS applies
its own rounded mask to apple-touch-icon, and Android launchers mask
too. Feeding a pre-rounded square into a rounder mask leaves dark
slivers in the corners. The rounded artboard is for places that show
the icon unmasked — a favicon, a flat listing.
"""
import os
import re
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
BG = (12, 12, 20)
RING = (240, 168, 132)
CORE = (251, 228, 220)
SIZES = (180, 192, 512)
SS = 4                      # supersample; PIL strokes are aliased


def parse(svg_path):
    """(rings, core_r, stroke, half) in the artboard's own units."""
    svg = open(svg_path, encoding="utf-8").read()
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    half = float(box.group(1)) / 2
    rings = []
    for d in re.findall(r'<path[^>]*\sd="([^"]+)"', svg):
        pts = [(float(x), float(y)) for x, y in
               re.findall(r'[ML](-?[\d.]+)\s+(-?[\d.]+)', d)]
        if len(pts) > 8:
            rings.append(pts)
    core = float(re.search(r'<circle[^>]*\sr="([\d.]+)"', svg).group(1))
    stroke = float(re.search(r'stroke-width="([\d.]+)"', svg).group(1))
    rings.sort(key=lambda p: -max(abs(x) for x, _ in p))
    return rings, core, stroke, half


def _r(pts):
    """Mean radius of a ring, for offsetting its band evenly."""
    return sum((x * x + y * y) ** 0.5 for x, y in pts) / len(pts)


def render(svg_path, size, radius_frac=0.0):
    rings, core_r, stroke, half = parse(svg_path)
    img = Image.new("RGB", (size * SS, size * SS), BG)
    d = ImageDraw.Draw(img)
    k = (size * SS / 2) / half          # artboard units -> pixels
    c = size * SS / 2
    # Each ring is a BAND, not a stroked polyline: PIL draws a joint at
    # every vertex, and with 160 vertices and a stroke ~9% of the radius
    # those joints show as nicks all the way round. Offsetting each
    # point radially by half the stroke gives a clean closed band — and
    # because the rings nest, filling outer-then-inner in order lets the
    # next ring paint over the hole the previous one punched.
    hw = stroke / 2.0
    for pts in rings:
        for scale, colour in ((1.0 + hw / _r(pts), RING),
                              (1.0 - hw / _r(pts), BG)):
            d.polygon([(c + x * k * scale, c + y * k * scale)
                       for x, y in pts], fill=colour)
    r = core_r * k
    d.ellipse([c - r, c - r, c + r, c + r], fill=CORE)
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    if radius_frac:                     # rounded artboard: mask the corners
        mask = Image.new("L", (size * SS, size * SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size * SS - 1, size * SS - 1],
            radius=radius_frac * size * SS, fill=255)
        out = Image.new("RGB", (size, size), (0, 0, 0))
        out.paste(img, (0, 0), mask.resize((size, size),
                                           Image.Resampling.LANCZOS))
        img = out
    return img


def main(argv):
    rounded = "--rounded" in argv
    name = ("vibb-appikon-natt-rund.svg" if rounded
            else "vibb-appikon-natt-skarp.svg")
    src = os.path.join(HERE, name)
    frac = 12.6 / 56 if rounded else 0.0
    for size in SIZES:
        out = os.path.join(WEB, f"icon-{size}.png")
        render(src, size, frac).save(out, "PNG", optimize=True)
        print(f"  {os.path.relpath(out)}  ({size}px from {name})")


if __name__ == "__main__":
    main(sys.argv)
