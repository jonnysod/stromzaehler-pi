# Vorlage für config.py
# Kopiere diese Datei zu config.py und trage deine eigenen Werte ein:
#   cp config.example.py config.py
# config.py selbst wird nicht committed (siehe .gitignore).

# ---------------------------------------------------------------------------
# Pfade (Daten-Repo und flüchtige Arbeitsdateien)
# ---------------------------------------------------------------------------

# Absoluter Pfad zum lokalen Klon des Daten-Repos (z.B. stromzaehler-daten)
DATA_REPO_DIR = "/home/jonny/stromzaehler-daten"

# Ring-Puffer: Frame-JSON (gitignored, flüchtig)
RING_BUFFER_PATH = "/home/jonny/stromzaehler-daten/tick_state.json"

# Grenzfoto: von tick(keep_photo=True) hier abgelegt, von consolidate übernommen
KEEP_PHOTO_PATH = "/home/jonny/stromzaehler-daten/boundary.jpg"

# Rohframe-Log: JSONL, eine Zeile pro Frame (gitignored, rotierend ~1 MB)
RAW_FRAMES_LOG_PATH = "/home/jonny/stromzaehler-daten/raw_frames.jsonl"

# ---------------------------------------------------------------------------
# Ring-Puffer & Konsolidierung
# ---------------------------------------------------------------------------

# Maximale Anzahl Frames im Ring-Puffer (Reserve: ~45 min, läuft cron-unabhängig)
RING_BUFFER_SIZE = 45

# Wie viele der jüngsten Frames consolidate() für den Vote heranzieht
CONSOLIDATE_LOOKBACK = 30

# ---------------------------------------------------------------------------
# Debug: Box-Crops (nur während der Kalibrierung aktivieren)
# ---------------------------------------------------------------------------

# Setzt auf True, um pro Frame 7 TIF-Crops zu schreiben (SD-Verschleiß beachten!)
DEBUG_BOX_CROPS = False

# Zielverzeichnis für die TIF-Crops (gitignored); Retention via systemd-tmpfiles
DEBUG_BOX_CROPS_DIR = "/home/jonny/stromzaehler-daten/debug_crops"

# ---------------------------------------------------------------------------
# Kipp-Regel (Anker-Revision im Tail-Entrollen, siehe plan-consolidate-ergaenzung.md)
# ---------------------------------------------------------------------------

# "Gut erkannt": ein Read zählt für die Treppe nur ab dieser Tesseract-Konfidenz.
# Bewusst niedrig - korrekte Z-Reads haben bei Unschärfe oft conf ~5, Fehlreads
# dagegen oft conf 0. An echten Daten nachtunen.
KIP_MIN_CONFIDENCE = 5

# So viele konsekutive, untereinander monotone Widerspruchs-Frames kippen den Anker.
KIP_THRESHOLD = 3

# Angenommene maximale Hauslast in Watt - bestimmt das Schritt-Limit pro Frame
# (ceil(MAX_POWER_W * elapsed_min / 6000), 1-Minuten-Floor gegen NTP-Rückwärtssprünge).
# Anheben bei Wallbox/Wärmepumpe.
MAX_POWER_W = 10000

# ---------------------------------------------------------------------------
# Plausibilität & Startwert
# ---------------------------------------------------------------------------

# Manuell abgelesener Startwert als Wh-Integer, falls noch keine (plausible)
# Messung existiert oder die letzte außerhalb von SEARCH_DAYS_FOR_PLAUSIBLE_DATA liegt.
# Beispiel: Ablesung 52983.8 kWh → siebenstellig 0529838 → 52983800 Wh
INITIAL_VALUE = 0
INITIAL_VALUE_TIMESTAMP = "2026-01-01T00:00:00Z"

# Maximal plausibler Anstieg in Wh pro vollen 15 Minuten (proportional skaliert)
# 5 kWh = 5000 Wh pro Viertelstunde (großzügig für kurze Ausreißer)
MAX_PLAUSIBLE_INCREASE_PER_15_MIN = 5000

# Wie viele Tage rückwirkend nach der letzten plausiblen Messung gesucht wird.
SEARCH_DAYS_FOR_PLAUSIBLE_DATA = 7
