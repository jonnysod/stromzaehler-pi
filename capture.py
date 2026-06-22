#!/usr/bin/env python3
import subprocess
import time
import json
import hashlib
import os
from datetime import datetime, timedelta, timezone
import RPi.GPIO as GPIO
from config import (
    DATA_REPO_DIR,
    INITIAL_VALUE,
    INITIAL_VALUE_TIMESTAMP,
    MAX_PLAUSIBLE_INCREASE_PER_15_MIN,
    SEARCH_DAYS_FOR_PLAUSIBLE_DATA,
)

LED_PIN = 17
DATA_DIR = os.path.join(DATA_REPO_DIR, "data")

# Ordnername: Basic-Format (keine Trennzeichen, dateisystemsicher)
FOLDER_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"
# entry.json: Extended-Format (lesbarer, gültiges ISO 8601)
ENTRY_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
# Beide immer in UTC, "Z" wird beim Schreiben manuell angehängt.
# Beim Parsen erkennt strptime mit "%z" das literale "Z" seit Python 3.7
# automatisch als UTC-Offset und liefert ein zeitzonenbewusstes datetime.

MAX_ATTEMPTS = 3


def format_folder_timestamp(dt):
    return dt.strftime(FOLDER_TIMESTAMP_FORMAT) + "Z"


def format_entry_timestamp(dt):
    return dt.strftime(ENTRY_TIMESTAMP_FORMAT) + "Z"


def parse_folder_timestamp(value):
    return datetime.strptime(value, FOLDER_TIMESTAMP_FORMAT + "%z")


def parse_entry_timestamp(value):
    return datetime.strptime(value, ENTRY_TIMESTAMP_FORMAT + "%z")


def take_photo(entry_dir):
    image_raw = os.path.join(entry_dir, "foto.jpg")
    image_ocr = os.path.join(entry_dir, "ocr.jpg")

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LED_PIN, GPIO.OUT)

    try:
        GPIO.output(LED_PIN, True)
        time.sleep(0.5)

        subprocess.run([
            "rpicam-still",
            "--autofocus-mode", "manual",
            "--lens-position", "22",
            "--rotation", "180",
            "--width", "2304",
            "--height", "1296",
            "--quality", "80",
            "-o", image_raw
        ], check=True)

    finally:
        GPIO.output(LED_PIN, False)
        GPIO.cleanup()

    subprocess.run([
        "convert", image_raw,
        "-crop", "1350x290+50+420",
        "-level", "3%,38%",
        "-negate",
        "-colorspace", "Gray",
        image_ocr
    ], check=True)

    result = subprocess.run(
        ["tesseract", image_ocr, "stdout",
         "--psm", "8",
         "-c", "tessedit_char_whitelist=0123456789"],
        capture_output=True, text=True, check=True
    )

    value = result.stdout.strip()
    return image_raw, value


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def get_previous_entry():
    # Geht von neuestem zu ältestem Ordner durch und überspringt dabei
    # Ordner ohne (oder noch ohne) entry.json - z.B. den gerade erst
    # angelegten eigenen Ordner, oder Reste eines abgebrochenen Laufs.
    # Wichtig: muss aufgerufen werden, BEVOR der neue entry_dir angelegt
    # wird, sonst würde dieser sich selbst als "vorherigen" Eintrag sehen.
    if not os.path.isdir(DATA_DIR):
        return None

    entries = sorted(os.listdir(DATA_DIR), reverse=True)

    for entry_name in entries:
        entry_file = os.path.join(DATA_DIR, entry_name, "entry.json")
        if os.path.isfile(entry_file):
            with open(entry_file) as f:
                return json.load(f)

    return None


def bootstrap_entry():
    if INITIAL_VALUE is None:
        return None
    return {"value_raw": INITIAL_VALUE, "timestamp": INITIAL_VALUE_TIMESTAMP}


def get_last_plausible_entry():
    last_plausible = bootstrap_entry()

    if not os.path.isdir(DATA_DIR):
        return last_plausible

    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_DAYS_FOR_PLAUSIBLE_DATA)
    entries = sorted(os.listdir(DATA_DIR))

    for entry_name in entries:
        try:
            entry_time = parse_folder_timestamp(entry_name)
        except ValueError:
            continue  # Ordnername entspricht nicht dem erwarteten Format

        if entry_time < cutoff:
            continue  # außerhalb des Suchfensters, Datei wird gar nicht erst geöffnet

        entry_file = os.path.join(DATA_DIR, entry_name, "entry.json")
        if not os.path.isfile(entry_file):
            continue

        with open(entry_file) as f:
            data = json.load(f)

        value = data.get("value_raw", "")
        timestamp = data.get("timestamp", "")

        if check_plausibility(value, timestamp, last_plausible):
            last_plausible = data

    return last_plausible


def check_plausibility(value, current_timestamp, previous_entry):
    if len(value) != 6 or not value.isdigit():
        return False

    if previous_entry is None:
        return True

    previous_value = previous_entry.get("value_raw", "")
    if len(previous_value) != 6 or not previous_value.isdigit():
        return True  # vorheriger Wert selbst nicht verlässlich, kein Vergleich möglich

    previous_timestamp = previous_entry.get("timestamp")
    try:
        previous_time = parse_entry_timestamp(previous_timestamp)
        current_time = parse_entry_timestamp(current_timestamp)
    except (TypeError, ValueError):
        return True  # Zeitstempel fehlt/ungültig, kein zeitbasierter Vergleich möglich

    elapsed_minutes = max((current_time - previous_time).total_seconds() / 60, 1)
    max_allowed_increase = MAX_PLAUSIBLE_INCREASE_PER_15_MIN * (elapsed_minutes / 15)

    diff = int(value) - int(previous_value)
    return 0 <= diff <= max_allowed_increase


def check_plausibility_relaxed(value, current_timestamp, last_plausible_entry):
    """Fallback-Prüfung bei wiederholtem plausible=false mit gleichem Wert.

    Prüft ob der Wert ohne die letzte Stelle (10er-Genauigkeit) gegen den
    letzten plausiblen Wert ohne letzte Stelle plausibel wäre. Fängt den
    Rollenzähler-Fehlerfall ab, bei dem ein Misread einer im Übergang
    befindlichen letzten Stelle einen korrekten Folgewert blockiert
    (z.B. Misread "052993" ankert sich, korrekte "052992" werden abgelehnt).

    Wird nur aufgerufen, wenn:
    - der vorherige Eintrag plausible=false hat
    - der vorherige Wert identisch zum aktuellen Wert ist
    - der normale Check bereits fehlgeschlagen ist
    """
    if not last_plausible_entry:
        return False

    truncated_value = value[:-1]
    truncated_prev = {
        "value_raw": last_plausible_entry["value_raw"][:-1],
        "timestamp": last_plausible_entry["timestamp"],
    }

    # check_plausibility erwartet 6-stellige Werte - wir übergeben hier
    # bewusst 5-stellige, die Längenprüfung schlägt also fehl. Daher
    # direkt die Kernlogik replizieren ohne Längencheck.
    if not truncated_value.isdigit() or not truncated_prev["value_raw"].isdigit():
        return False

    previous_timestamp = truncated_prev["timestamp"]
    try:
        previous_time = parse_entry_timestamp(previous_timestamp)
        current_time = parse_entry_timestamp(current_timestamp)
    except (TypeError, ValueError):
        return False

    elapsed_minutes = max((current_time - previous_time).total_seconds() / 60, 1)
    max_allowed_increase = MAX_PLAUSIBLE_INCREASE_PER_15_MIN * (elapsed_minutes / 15)

    diff = int(truncated_value) - int(truncated_prev["value_raw"])
    return 0 <= diff <= max_allowed_increase


def write_entry(timestamp, entry_dir, image_raw, value, plausible, previous_entry, note=None):
    image_hash = sha256_of_file(image_raw)
    prev_hash = previous_entry["entry_hash"] if previous_entry else None

    entry = {
        "timestamp": timestamp,
        "value_raw": value,
        "plausible": plausible,
        "image_hash": image_hash,
        "prev_hash": prev_hash,
    }

    if note is not None:
        entry["note"] = note

    entry_string = json.dumps(entry, sort_keys=True)
    entry_hash = hashlib.sha256(entry_string.encode()).hexdigest()
    entry["entry_hash"] = entry_hash

    entry_file = os.path.join(entry_dir, "entry.json")
    with open(entry_file, "w") as f:
        json.dump(entry, f, indent=2)

    return entry_file


def git_commit_and_push(timestamp, entry_dir):
    subprocess.run(["git", "add", entry_dir], cwd=DATA_REPO_DIR, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Messung {timestamp}"],
        cwd=DATA_REPO_DIR, check=True
    )
    subprocess.run(["git", "push"], cwd=DATA_REPO_DIR, check=True)


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    folder_timestamp = format_folder_timestamp(now)
    entry_timestamp = format_entry_timestamp(now)

    # Reihenfolge wichtig: previous_entry ermitteln, BEVOR entry_dir
    # angelegt wird (siehe Kommentar in get_previous_entry()).
    previous_entry = get_previous_entry()          # für Hash-Chain (lückenlos)
    last_plausible_entry = get_last_plausible_entry()  # für Wertevergleich

    entry_dir = os.path.join(DATA_DIR, folder_timestamp)
    os.makedirs(entry_dir, exist_ok=True)

    note = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        image_raw, value = take_photo(entry_dir)
        plausible = check_plausibility(value, entry_timestamp, last_plausible_entry)
        print(f"Versuch {attempt}: Wert={value} plausibel={plausible}")

        if plausible:
            break

    if not plausible:
        prev_not_plausible = previous_entry and not previous_entry.get("plausible")
        prev_same_value = previous_entry and previous_entry.get("value_raw") == value
        if prev_not_plausible and prev_same_value:
            if check_plausibility_relaxed(value, entry_timestamp, last_plausible_entry):
                plausible = True
                note = "relaxed_truncation"
                print(f"Relaxed-Check greift: {value} akzeptiert (note: relaxed_truncation)")

    entry_file = write_entry(entry_timestamp, entry_dir, image_raw, value, plausible, previous_entry, note)
    git_commit_and_push(entry_timestamp, entry_dir)

    print(f"Zählerstand: {value} (plausibel: {plausible}{f', note: {note}' if note else ''})")
    print(f"Committed und gepusht: {entry_timestamp}")
