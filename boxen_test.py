#!/usr/bin/env python3
"""
Kalibrier-Helfer fuer die Ziffern-Segmentierung.

Schneidet aus einem Foto die einzelnen Ziffern-Boxen heraus und erzeugt:
  <name>_overlay.png  - Foto mit eingezeichneten Boxen   (Lage justieren)
  <name>_boxen.png    - alle Boxen einzeln nebeneinander  (Inhalt beurteilen)

Workflow: laufen lassen, Bilder anschauen, Zahlen in BOXES anpassen,
wiederholen, bis jede Ziffer mittig und vollstaendig in ihrer Box sitzt.

Aufruf:
    python3 boxen_test.py foto.jpg          # nur Bilder
    python3 boxen_test.py foto.jpg --ocr    # zusaetzlich Tesseract pro Box
"""

import os
import sys
import math
import shutil
import tempfile
import subprocess

# ---------------------------------------------------------------------------
# Boxen in VOLLER Bildkoordinate, exakt wie ImageMagick -crop WxH+X+Y.
# x,y = linke obere Ecke. Nur diese Zahlen anfassen.
#
# level: None      -> -auto-level (gut, um zuerst nur die LAGE zu beurteilen)
#        "8%,30%"  -> festes -level, sobald die Lage stimmt und du pro Box
#                     die echten Helligkeitswerte misst (siehe MESSEN unten)
# rot:   0         -> kein Drehen
#        3         -> 3 Grad GEGEN den Uhrzeigersinn vor dem Zuschnitt
#        -3        -> 3 Grad IM Uhrzeigersinn
#                     (gedreht wird um den Mittelpunkt der jeweiligen Box)
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

# Nur fuers Overlay: hebt das dunkle Display-Feld an, damit man die Ziffern
# unter den Boxen ueberhaupt sieht. Beeinflusst die Boxen selbst NICHT.
DISPLAY_LEVEL = "0%,55%"

# Tesseract Page-Segmentation-Mode fuer eine Einzelbox. Zum Experimentieren:
#   "10" = ein Einzelzeichen (theoretisch passend, aber bei Unschaerfe zickig)
#   "8"  = ein Wort (oft robuster, auch wenn nur eine Ziffer drin ist)
#   "13" = rohe Zeile, ohne Tesseract-eigene Vorsegmentierung
PSM = "10"

# Auf dem Pi heisst der Befehl 'convert' (IM6), auf dem Mac per brew 'magick' (IM7).
MAGICK = "magick" if shutil.which("magick") else "convert"

# Zwischendateien NICHT hart nach /tmp schreiben: auf macOS ist /tmp fuer
# Homebrew-Binaries (Tesseract) gesperrt ("failed to open"). tempfile nimmt
# das benutzereigene Temp-Verzeichnis ($TMPDIR), auf dem Pi weiterhin /tmp.
TMP = tempfile.gettempdir()


def _find_font():
    """Erste vorhandene Schrift finden. ImageMagick 7 auf dem Mac (brew) hat
    oft KEINE Default-Font gesetzt -> '-draw text' bricht ab. Darum explizit
    angeben. macOS und Linux/Pi haben unterschiedliche Pfade."""
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",    # macOS
        "/System/Library/Fonts/Helvetica.ttc",             # macOS (praktisch immer da)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # Debian / Raspberry Pi OS
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if os.path.isfile(path):
            return path
    return None  # keine gefunden -> Labels werden weggelassen, Boxen trotzdem gezeichnet


FONT = _find_font()


def geom(b):
    return f"{b['w']}x{b['h']}+{b['x']}+{b['y']}"


def crop_box(src, b, out):
    """Schneidet Box b aus src nach out (rohes Graustufen-PNG, ungelevelt).
    Bei rot != 0 wird vorher um den Box-Mittelpunkt gedreht; positiver Winkel
    = gegen den Uhrzeigersinn."""
    rot = b.get("rot", 0)
    if not rot:
        subprocess.run([MAGICK, src, "-crop", geom(b), "+repage", out], check=True)
        return
    # Grosszuegig quadratisch um den Box-Mittelpunkt schneiden (halbe Diagonale
    # als Rand, damit beim Drehen keine Ecke fehlt), dann drehen, dann die
    # eigentliche Box aus der Mitte herausschneiden.
    pad = int(math.hypot(b["w"], b["h"]) / 2) + 2
    cx, cy = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
    side = pad * 2
    bx, by = pad - b["w"] // 2, pad - b["h"] // 2
    subprocess.run([
        MAGICK, src,
        "-crop", f"{side}x{side}+{cx - pad}+{cy - pad}", "+repage",
        "-virtual-pixel", "black",
        "-distort", "SRT", str(-rot),   # SRT positiv = im UZS -> fuer CCW negieren
        "+repage",
        "-crop", f"{b['w']}x{b['h']}+{bx}+{by}", "+repage",
        out,
    ], check=True)


def make_overlay(src, out):
    cmd = [
        MAGICK, src, "-level", DISPLAY_LEVEL,
        "-fill", "none", "-stroke", "#00ff66", "-strokewidth", "2",
    ]
    for b in BOXES:
        rot = b.get("rot", 0)
        if rot:
            # Die Box, die im Originalbild dem (CCW-)gedrehten Ausschnitt
            # entspricht, ist um den Mittelpunkt im UZS gedreht. Ecken explizit
            # rechnen (Bildschirm-Koordinaten, y nach unten):
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
    # Gleiche Pipeline wie capture.py, nur pro Box und mit PSM (Einzelzeichen).
    # Eingabe fuer Tesseract als sauberes 8-Bit-TIFF ohne Profil: ImageMagick 7
    # (magick) schreibt PNGs, die die Tesseract-Bibliothek (Leptonica) auf manchen
    # Systemen nicht einlesen kann -> leere Ausgabe. TIFF + -depth 8 -strip umgeht das.
    raw = os.path.join(TMP, f"_raw_{b['name']}.png")
    crop_box(src, b, raw)
    level = ["-auto-level"] if b["level"] is None else ["-level", b["level"]]
    tmp = os.path.join(TMP, f"_ocr_{b['name']}.tif")
    subprocess.run([MAGICK, raw, *level, "-negate", "-colorspace", "Gray",
                    "-depth", "8", "-strip", tmp], check=True)
    os.remove(raw)
    res = subprocess.run(
        ["tesseract", tmp, "stdout", "--psm", PSM,
         "-c", "tessedit_char_whitelist=0123456789"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    os.remove(tmp)
    return res.stdout.strip()


def main():
    if len(sys.argv) < 2:
        print("Aufruf: python3 boxen_test.py <foto.jpg> [--ocr]")
        sys.exit(1)

    src = sys.argv[1]
    do_ocr = "--ocr" in sys.argv[2:]
    if not os.path.isfile(src):
        print(f"Datei nicht gefunden: {src}")
        sys.exit(1)

    base = os.path.splitext(os.path.basename(src))[0]
    overlay, contact = f"{base}_overlay.png", f"{base}_boxen.png"

    make_overlay(src, overlay)
    make_contact(src, contact)
    print(f"geschrieben: {overlay}  (Foto mit eingezeichneten Boxen)")
    print(f"geschrieben: {contact}  (Boxen einzeln nebeneinander)")

    if do_ocr:
        print(f"\nOCR pro Box (Tesseract --psm {PSM}):")
        reads = [(b["name"], ocr_box(src, b)) for b in BOXES]
        for name, val in reads:
            print(f"  {name}: {val!r}")
        print("  ergibt: " + "".join(v if v else "?" for _, v in reads))


# ---------------------------------------------------------------------------
# MESSEN der level-Werte pro Box (wenn die Lage stimmt), pro Box einzeln:
#   convert foto.jpg -crop 140x250+520+452 +repage -colorspace Gray \
#     -format "%[fx:100*minima] %[fx:100*maxima]" info:
# Ausgabe z.B. "4.1 36.8" -> level: "4%,37%" in der jeweiligen Box eintragen.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
