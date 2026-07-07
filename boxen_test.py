#!/usr/bin/env python3
"""
Calibration helper for digit segmentation.

Crops the individual digit boxes from a photo and produces:
  <name>_overlay.png  - photo with boxes drawn on it   (adjust placement)
  <name>_boxen.png    - all boxes side by side          (judge content)

Workflow: run, look at the images, tweak BOXES, repeat until every digit
sits centered and complete in its box.

Usage:
    python3 boxen_test.py foto.jpg          # images only
    python3 boxen_test.py foto.jpg --ocr    # plus Tesseract per box
"""

import math
import os
import subprocess
import sys

from boxen import BOXES, MAGICK, TMP, crop_box, process_box

# Overlay only: lifts the dark display field so the digits under the boxes
# are visible. Does NOT affect the boxes themselves.
DISPLAY_LEVEL = "0%,55%"

# Tesseract page-segmentation mode for a single box. To experiment:
#   "10" = single char (fits in theory, but fussy when blurry)
#   "8"  = single word (often more robust, even with one digit)
#   "13" = raw line, no Tesseract pre-segmentation
PSM = "10"


def _find_font():
    """First available font. ImageMagick 7 on Mac (brew) often has NO default
    font -> '-draw text' aborts. Set it explicitly; macOS and Linux/Pi differ."""
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",    # macOS
        "/System/Library/Fonts/Helvetica.ttc",             # macOS (almost always there)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # Debian / Pi OS
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if os.path.isfile(path):
            return path
    return None  # none found -> labels dropped, boxes still drawn


FONT = _find_font()


def make_overlay(src, out):
    cmd = [
        MAGICK, src, "-level", DISPLAY_LEVEL,
        "-fill", "none", "-stroke", "#00ff66", "-strokewidth", "2",
    ]
    for b in BOXES:
        rot = b.get("rot", 0)
        if rot:
            # The box matching the CCW-rotated crop is rotated CW around its
            # center in the original. Compute corners explicitly (screen
            # coords, y down):
            cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
            hw, hh = b["w"] / 2, b["h"] / 2
            a = math.radians(rot)
            cos, sin = math.cos(a), math.sin(a)
            pts = []
            for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
                px = cx + dx * cos - dy * sin
                py = cy + dx * sin + dy * cos
                pts.append(f"{px:.0f},{py:.0f}")
            cmd += ["-draw", "polygon " + " ".join(pts)]
        else:
            cmd += ["-draw",
                    f"rectangle {b['x']},{b['y']} {b['x'] + b['w']},{b['y'] + b['h']}"]
    if FONT:
        labels = " ".join(f"text {b['x'] + 4},{b['y'] - 8} '{b['name']}'" for b in BOXES)
        cmd += ["-stroke", "none", "-fill", "yellow",
                "-font", FONT, "-pointsize", "28", "-draw", labels]
    cmd.append(out)
    subprocess.run(cmd, check=True)


def make_contact(src, out):
    # Deliberately pad-free: the contact sheet answers "is the box on the
    # digit", not "does Tesseract have a quiet zone" (that's process_box).
    tiles = []
    for b in BOXES:
        raw = os.path.join(TMP, f"_raw_{b['name']}.png")
        crop_box(src, b, raw)
        tile = os.path.join(TMP, f"_box_{b['name']}.png")
        level = ["-auto-level"] if b["level"] is None else ["-level", b["level"]]
        label = b["name"] + (f"  {b['rot']:+d}" if b.get("rot") else "")
        cmd = [
            MAGICK, raw, "-colorspace", "Gray", *level, "-colorspace", "sRGB",
            "-bordercolor", "#00ff66", "-border", "2",
            "-background", "#222222", "-gravity", "North", "-splice", "0x26",
        ]
        if FONT:
            cmd += ["-fill", "yellow", "-font", FONT,
                    "-pointsize", "20", "-annotate", "+0+3", label]
        cmd.append(tile)
        subprocess.run(cmd, check=True)
        os.remove(raw)
        tiles.append(tile)
    subprocess.run([
        MAGICK, *tiles, "+append",
        "-background", "#222222", "-bordercolor", "#222222", "-border", "6", out,
    ], check=True)
    for t in tiles:
        os.remove(t)


def ocr_box(src, b):
    # Image pipeline (crop/rotate/level/pad/negate) comes from
    # boxen.process_box - one source of truth so the calibration read matches
    # the live read exactly. Only the PSM stays variable here.
    tif = process_box(src, b, TMP)
    res = subprocess.run(
        ["tesseract", tif, "stdout", "--psm", PSM,
         "-c", "tessedit_char_whitelist=0123456789"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    os.remove(tif)
    return res.stdout.strip()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 boxen_test.py <foto.jpg> [--ocr]")
        sys.exit(1)

    src = sys.argv[1]
    do_ocr = "--ocr" in sys.argv[2:]
    if not os.path.isfile(src):
        print(f"File not found: {src}")
        sys.exit(1)

    base = os.path.splitext(os.path.basename(src))[0]
    overlay, contact = f"{base}_overlay.png", f"{base}_boxen.png"

    make_overlay(src, overlay)
    make_contact(src, contact)
    print(f"written: {overlay}  (photo with boxes drawn)")
    print(f"written: {contact}  (boxes side by side)")

    if do_ocr:
        print(f"\nOCR per box (Tesseract --psm {PSM}):")
        reads = [(b["name"], ocr_box(src, b)) for b in BOXES]
        for name, val in reads:
            print(f"  {name}: {val!r}")
        print("  result: " + "".join(v if v else "?" for _, v in reads))


# ---------------------------------------------------------------------------
# MEASURING level values per box (once placement is right), one box at a time:
#   convert foto.jpg -crop 140x250+520+452 +repage -colorspace Gray \
#     -format "%[fx:100*minima] %[fx:100*maxima]" info:
# Output e.g. "4.1 36.8" -> level: "4%,37%" in that box.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
