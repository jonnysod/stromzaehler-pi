#!/bin/bash
# Misst Minima/Maxima-Helligkeit pro Box und gibt level-Strings aus.
# Aufruf: bash measure_levels.sh foto.jpg

FOTO="${1:-test.jpg}"

if [ ! -f "$FOTO" ]; then
  echo "Datei nicht gefunden: $FOTO"
  exit 1
fi

echo "Foto: $FOTO"
echo ""
echo "Box         x     y     w     h     min%   max%   level"
echo "--------------------------------------------------------"

python3 - "$FOTO" << 'PYEOF'
import subprocess, sys

BOXES = [
    {"name": "d1", "x":   74, "y": 452, "w": 135, "h": 250},
    {"name": "d2", "x":  302, "y": 452, "w": 135, "h": 250},
    {"name": "d3", "x":  520, "y": 452, "w": 140, "h": 250},
    {"name": "d4", "x":  747, "y": 442, "w": 153, "h": 250},
    {"name": "d5", "x":  980, "y": 452, "w": 144, "h": 250},
    {"name": "d6", "x": 1210, "y": 452, "w": 139, "h": 250},
    {"name":  "Z", "x": 1459, "y": 470, "w": 130, "h": 240},
]

foto = sys.argv[1]

for b in BOXES:
    crop = f"{b['w']}x{b['h']}+{b['x']}+{b['y']}"
    r = subprocess.run(
        ["convert", foto, "-crop", crop, "+repage",
         "-colorspace", "Gray",
         "-format", "%[fx:100*minima] %[fx:100*maxima]", "info:"],
        capture_output=True, text=True
    )
    parts = r.stdout.strip().split()
    if len(parts) == 2:
        lo = float(parts[0])
        hi = float(parts[1])
        level = f'"{lo:.0f}%,{hi:.0f}%"'
        print(f"  {b['name']:<6}  {b['x']:>5} {b['y']:>5} {b['w']:>5} {b['h']:>5}   {lo:>5.1f}  {hi:>5.1f}   {level}")
    else:
        print(f"  {b['name']:<6}  Fehler: {r.stderr.strip()}")

print()
print("BOXES-Einträge (level-Werte zum Eintragen in boxen.py):")
print()
for b in BOXES:
    crop = f"{b['w']}x{b['h']}+{b['x']}+{b['y']}"
    r = subprocess.run(
        ["convert", foto, "-crop", crop, "+repage",
         "-colorspace", "Gray",
         "-format", "%[fx:100*minima] %[fx:100*maxima]", "info:"],
        capture_output=True, text=True
    )
    parts = r.stdout.strip().split()
    if len(parts) == 2:
        lo = float(parts[0])
        hi = float(parts[1])
        level = f'"{lo:.0f}%,{hi:.0f}%"'
        print(f'  {{"name": "{b["name"]}", "x": {b["x"]:>5}, "y": {b["y"]}, "w": {b["w"]}, "h": {b["h"]}, "level": {level}}}')
PYEOF
