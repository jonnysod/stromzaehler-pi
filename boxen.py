#!/usr/bin/env python3
"""
Single source of truth for box calibration and digit OCR.

Exports:
  BOXES         - calibrated box definitions (7 digits)
  MAGICK        - ImageMagick binary ('magick' on Mac, 'convert' on Pi)
  TMP           - platform-safe temp dir (not hardcoded /tmp)
  crop_box()    - canonical crop + rotate around box center
  process_box() - crop + level + pad + negate -> OCR-ready TIFF (shared)
  read_boxes()  - photo -> {boxname: (char, confidence)}
"""
import math
import os
import shutil
import subprocess
import tempfile

# ---------------------------------------------------------------------------
# Box calibration (single edit point)
# ---------------------------------------------------------------------------
# Coords: full-image, like ImageMagick -crop WxH+X+Y.
#   d1-d6 = six black digits (whole kWh, left -> right)
#   Z     = red tenths digit (0.1 kWh = 100 Wh)
#
# level: None   -> -auto-level
#        string -> -level <string>, e.g. "3%,38%"
# rot:   rotation around box center BEFORE crop. Positive = CCW.
# pad:   black border (px) added after level, before negate. Forces a quiet
#        zone so psm-10 doesn't merge an edge-filling digit with the image
#        border (such digits otherwise read empty).
# ---------------------------------------------------------------------------
BOXES = [
    {"name": "d1", "x":   49, "y": 497, "w": 155, "h": 270, "level": None, "rot": 0, "pad": 40},
    {"name": "d2", "x":  277, "y": 467, "w": 155, "h": 290, "level": None, "rot": 0, "pad": 0},
    {"name": "d3", "x":  495, "y": 467, "w": 160, "h": 290, "level": None, "rot": 0, "pad": 0},
    {"name": "d4", "x":  722, "y": 457, "w": 173, "h": 290, "level": None, "rot": 0, "pad": 0},
    {"name": "d5", "x":  955, "y": 467, "w": 164, "h": 280, "level": None, "rot": 0, "pad": 20},
    {"name": "d6", "x": 1185, "y": 467, "w": 159, "h": 290, "level": None, "rot": 0, "pad": 0},
    {"name": "Z",  "x": 1457, "y": 470, "w": 120, "h": 235, "level": None, "rot": 0, "pad": 0},
]

# ---------------------------------------------------------------------------
# Platform constants
# ---------------------------------------------------------------------------

# IM7 (magick) on Mac via brew, IM6 (convert) on the Pi.
MAGICK = "magick" if shutil.which("magick") else "convert"

# Don't hardcode /tmp: on macOS it's blocked for Homebrew binaries.
# tempfile picks $TMPDIR (Pi: /tmp, Mac: ~/...).
TMP = tempfile.gettempdir()


# ---------------------------------------------------------------------------
# Canonical crop + rotate
# ---------------------------------------------------------------------------

def crop_box(src, b, out):
    """Crop box b from src into out.

    With rot != 0 the full image is rotated generously around the box center
    before cropping, so no corner is lost to clipping. Positive rot = CCW.
    """
    rot = b.get("rot", 0)
    if not rot:
        subprocess.run(
            [MAGICK, src, "-crop", f"{b['w']}x{b['h']}+{b['x']}+{b['y']}",
             "+repage", out],
            check=True,
        )
        return

    # Crop a generous square around the center (half-diagonal margin) so no
    # corner is missing after rotation, rotate, then cut the real box.
    pad = int(math.hypot(b["w"], b["h"]) / 2) + 2
    cx, cy = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
    side = pad * 2
    bx, by = pad - b["w"] // 2, pad - b["h"] // 2
    subprocess.run(
        [
            MAGICK, src,
            "-crop", f"{side}x{side}+{cx - pad}+{cy - pad}", "+repage",
            "-virtual-pixel", "black",
            "-distort", "SRT", str(-rot),  # SRT positive = CW -> negate for CCW
            "+repage",
            "-crop", f"{b['w']}x{b['h']}+{bx}+{by}", "+repage",
            out,
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# OCR pipeline (shared with boxen_test.py)
# ---------------------------------------------------------------------------

def process_box(foto_path, box, tmp_dir):
    """Crop + rotate + level + pad + negate + grayscale -> 8-bit TIFF.

    Order matters:
      - pad AFTER level: pure black must not pin -auto-level's min to 0.
      - pad BEFORE negate: the black border flips to white and becomes the
        quiet zone Tesseract needs around the (now dark) digit.

    Returns the path to the generated TIFF.
    """
    raw = os.path.join(tmp_dir, f"{box['name']}_raw.png")
    crop_box(foto_path, box, raw)

    level = box.get("level")
    pad = box.get("pad", 0)

    args = [MAGICK, raw]
    args += ["-level", level] if level is not None else ["-auto-level"]
    if pad:
        args += ["-bordercolor", "black", "-border", str(pad)]
    args += ["-negate", "-colorspace", "Gray", "-depth", "8", "-strip"]

    out_path = os.path.join(tmp_dir, f"{box['name']}.tif")
    args.append(out_path)
    subprocess.run(
        args, check=True,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    os.unlink(raw)
    return out_path


def _ocr_box(tif_path):
    """Tesseract --psm 10 on the TIFF; returns (char, confidence).

    Confidence is a weight, not a gate: correct reads often have low
    confidence when blurry, misreads mostly 0. Empty = ("", 0).
    """
    result = subprocess.run(
        [
            "tesseract", tif_path, "stdout",
            "--psm", "10",
            "-c", "tessedit_char_whitelist=0123456789",
            "tsv",
        ],
        capture_output=True, encoding="utf-8", errors="replace",
    )

    best_char, best_conf = "", 0
    for line in result.stdout.strip().splitlines()[1:]:  # line 0 = TSV header
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            conf = int(parts[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        text = parts[11].strip() if len(parts) > 11 else ""
        if text and text in "0123456789" and conf > best_conf:
            best_char, best_conf = text, conf

    return best_char, best_conf


def read_boxes(foto_path, debug_label=None, debug_dir=None):
    """Read all 7 boxes from the photo.

    Params:
      foto_path   - path to the full original photo (JPG)
      debug_label - basic-format timestamp prefix, e.g. '20260625T143001Z'.
                    With debug_dir set, the leveled TIFF (exactly what
                    Tesseract saw) is copied to
                    {debug_dir}/{debug_label}_{boxname}.tif.
      debug_dir   - target dir for debug TIFFs; must exist.

    Returns:
      {boxname: (char, confidence)}
        char : "0".."9" or "" (empty is a valid state)
        conf : 0..100; 0 when empty

    Temp files are always cleaned up. Debug copies stay (retention via
    systemd-tmpfiles, not in code).
    """
    results = {}
    with tempfile.TemporaryDirectory(dir=TMP) as tmp_dir:
        for box in BOXES:
            tif_path = process_box(foto_path, box, tmp_dir)
            results[box["name"]] = _ocr_box(tif_path)
            if debug_label and debug_dir:
                dst = os.path.join(debug_dir, f"{debug_label}_{box['name']}.tif")
                shutil.copy2(tif_path, dst)
    return results
