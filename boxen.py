#!/usr/bin/env python3
"""
Quelle der Wahrheit für Box-Kalibrierung und Ziffern-OCR.

Exportiert:
  BOXES        – kalibrierte Box-Definitionen (7 Stellen)
  MAGICK       – ImageMagick-Befehl ('magick' auf Mac, 'convert' auf Pi)
  TMP          – plattformsicheres Temp-Verzeichnis (nicht hart /tmp)
  crop_box()   – kanonisches Crop + Drehen um Box-Mittelpunkt
  read_boxes() – Foto → {boxname: (zeichen, konfidenz)}
"""
import math
import os
import shutil
import subprocess
import tempfile

# ---------------------------------------------------------------------------
# Box-Kalibrierung (einziger Edit-Punkt)
# ---------------------------------------------------------------------------
# Koordinaten: volle Bildkoordinate, wie ImageMagick -crop WxH+X+Y.
#   d1–d6 = sechs schwarze Stellen (ganze kWh, links → rechts)
#   Z     = rote Zehntelstelle (0,1 kWh = 100 Wh)
#
# level: None   → -auto-level
#        String → -level <string>, z.B. "3%,38%"
# rot: Drehung um den Box-Mittelpunkt VOR dem Zuschnitt.
#      Positiver Winkel = CCW (gegen den Uhrzeigersinn).
# ---------------------------------------------------------------------------
BOXES = [
    {"name": "d1", "x":   74, "y": 452, "w": 135, "h": 250, "level": None, "rot": 0},
    {"name": "d2", "x":  302, "y": 452, "w": 135, "h": 250, "level": None, "rot": 0},
    {"name": "d3", "x":  520, "y": 452, "w": 140, "h": 250, "level": None, "rot": 0},
    {"name": "d4", "x":  747, "y": 442, "w": 153, "h": 250, "level": None, "rot": 0},
    {"name": "d5", "x":  980, "y": 452, "w": 144, "h": 250, "level": None, "rot": 2},
    {"name": "d6", "x": 1210, "y": 452, "w": 139, "h": 250, "level": None, "rot": 2},
    {"name": "Z",  "x": 1459, "y": 470, "w": 130, "h": 240, "level": None, "rot": 2},
]

# ---------------------------------------------------------------------------
# Plattform-Konstanten
# ---------------------------------------------------------------------------

# IM7 (magick) auf Mac via brew, IM6 (convert) auf dem Pi.
MAGICK = "magick" if shutil.which("magick") else "convert"

# Zwischendateien nicht hart nach /tmp: auf macOS ist /tmp für
# Homebrew-Binaries gesperrt. tempfile nimmt $TMPDIR (Pi: /tmp, Mac: ~/...)
TMP = tempfile.gettempdir()


# ---------------------------------------------------------------------------
# Kanonisches Crop + Drehen
# ---------------------------------------------------------------------------

def crop_box(src, b, out):
    """Schneidet Box b aus src und schreibt das Ergebnis nach out.

    Bei rot != 0 wird das Vollbild großzügig um den Box-Mittelpunkt gedreht,
    bevor die eigentliche Box ausgeschnitten wird. So gehen keine Ecken durch
    Clipping verloren.

    Positiver rot-Winkel = CCW (gegen den Uhrzeigersinn).
    """
    rot = b.get("rot", 0)
    if not rot:
        subprocess.run(
            [MAGICK, src, "-crop", f"{b['w']}x{b['h']}+{b['x']}+{b['y']}",
             "+repage", out],
            check=True,
        )
        return

    # Großzügig quadratisch um den Box-Mittelpunkt schneiden (halbe Diagonale
    # als Rand), damit beim Drehen keine Ecke fehlt, dann drehen, dann die
    # eigentliche Box aus der Mitte herausschneiden.
    pad = int(math.hypot(b["w"], b["h"]) / 2) + 2
    cx, cy = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
    side = pad * 2
    bx, by = pad - b["w"] // 2, pad - b["h"] // 2
    subprocess.run(
        [
            MAGICK, src,
            "-crop", f"{side}x{side}+{cx - pad}+{cy - pad}", "+repage",
            "-virtual-pixel", "black",
            "-distort", "SRT", str(-rot),  # SRT positiv = CW → negieren für CCW
            "+repage",
            "-crop", f"{b['w']}x{b['h']}+{bx}+{by}", "+repage",
            out,
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# OCR-Pipeline
# ---------------------------------------------------------------------------

def _process_box(foto_path, box, tmp_dir):
    """Crop + Drehen + Level + Negate + Graustufen → 8-Bit-TIFF.

    Verwendet crop_box() für den Crop/Dreh-Schritt.
    Gibt den Pfad zur erzeugten TIFF-Datei zurück.
    """
    raw = os.path.join(tmp_dir, f"{box['name']}_raw.png")
    crop_box(foto_path, box, raw)

    level = box.get("level")
    args = [MAGICK, raw]
    args += ["-level", level] if level is not None else ["-auto-level"]
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
    """Tesseract --psm 10 auf dem TIFF; gibt (zeichen, konfidenz) zurück.

    Konfidenz ist ein Gewicht, kein Tor: korrekte Reads haben bei Unschärfe
    oft niedrige Konfidenz, Fehlreads aber meist 0.
    Leerwert = ("", 0).
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
    for line in result.stdout.strip().splitlines()[1:]:  # Zeile 0 = TSV-Header
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
    """Liest alle 7 Boxen aus dem Foto.

    Parameter:
      foto_path   – Pfad zum vollen Originalfoto (JPG)
      debug_label – Zeitstempel-Präfix im Basic-Format, z.B. '20260625T143001Z'.
                    Ist dieser und debug_dir gesetzt, wird das gelevelte TIFF
                    (genau das, was Tesseract sah) nach
                    {debug_dir}/{debug_label}_{boxname}.tif kopiert.
      debug_dir   – Zielverzeichnis für Debug-TIFFs; muss existieren.

    Rückgabe:
      {boxname: (zeichen, konfidenz)}
        zeichen   : "0".."9" oder "" (Leerwert ist gültiger Zustand)
        konfidenz : 0..100; 0 bei Leerwert

    Temp-Dateien werden immer aufgeräumt. Debug-Copies bleiben liegen
    (Retention via systemd-tmpfiles, nicht per Code).
    """
    results = {}
    with tempfile.TemporaryDirectory(dir=TMP) as tmp_dir:
        for box in BOXES:
            tif_path = _process_box(foto_path, box, tmp_dir)
            results[box["name"]] = _ocr_box(tif_path)
            if debug_label and debug_dir:
                dst = os.path.join(debug_dir, f"{debug_label}_{box['name']}.tif")
                shutil.copy2(tif_path, dst)
    return results
