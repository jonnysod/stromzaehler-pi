#!/usr/bin/env python3
"""
Minutentakt-Sammler für den Stromzähler.

Aufruf via cron: * * * * * /usr/bin/python3 /home/jonny/stromzaehler-pi/tick.py

Pro Lauf:
  1. Exklusiven Lock holen (bei Konkurrenz: still beenden)
  2. Foto aufnehmen (IR-LED an/aus, rpicam-still) → Temp-Datei
  3. Alle 7 Boxen lesen (read_boxes aus boxen.py)
  4. Fenster-Statefile laden
  5. Zeitprüfung: Uhr rückwärts? → Frame verwerfen
  6. Fenster-Logik: anhängen oder rollen
  7. Statefile atomar zurückschreiben
  8. Temp-Foto verwerfen

Nichts wird committed. Kein entry.json. Das ist consolidate()-Aufgabe.

Dateipfade (relativ zu DATA_REPO_DIR aus config.py, gitignored):
  tick.lock          – Prozess-Lock
  tick_state.json    – offenes Fenster
  tick_archive.jsonl – abgeschlossene Fenster (eine JSON-Zeile pro Fenster)
"""
import fcntl
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import RPi.GPIO as GPIO

from boxen import read_boxes
from config import DATA_REPO_DIR

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
LED_PIN = 17

LOCK_PATH      = os.path.join(DATA_REPO_DIR, "tick.lock")
STATEFILE_PATH = os.path.join(DATA_REPO_DIR, "tick_state.json")
ARCHIVE_PATH   = os.path.join(DATA_REPO_DIR, "tick_archive.jsonl")

# Timestamp-Formate (konsistent mit capture.py)
_TS_FMT        = "%Y-%m-%dT%H:%M:%S"   # ohne Z

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt=_TS_FMT,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timestamp-Helfer
# ---------------------------------------------------------------------------

def _fmt(dt: datetime) -> str:
    """datetime → '2026-06-23T18:01:07Z'"""
    return dt.strftime(_TS_FMT) + "Z"


def _parse(s: str) -> datetime:
    """'2026-06-23T18:01:07Z' → timezone-aware UTC datetime."""
    return datetime.strptime(s, _TS_FMT + "%z")


def _floor_to_quarter(dt: datetime) -> datetime:
    """Rundet ein UTC-Datetime auf die volle Viertelstunde ab."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def _acquire_lock():
    """
    Versucht exklusiven Non-Blocking-Lock auf LOCK_PATH.
    Gibt das geöffnete File-Objekt zurück, oder None wenn belegt.
    Der Lock bleibt bis zum Prozessende / close() offen.
    """
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lf
    except BlockingIOError:
        lf.close()
        return None


# ---------------------------------------------------------------------------
# Foto aufnehmen
# ---------------------------------------------------------------------------

def _take_photo(path: str) -> None:
    """
    Nimmt ein volles Rohfoto auf und schreibt es nach `path`.
    IR-LED an → warten → rpicam-still → LED aus.
    GPIO wird immer aufgeräumt (finally).
    """
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LED_PIN, GPIO.OUT)
    try:
        GPIO.output(LED_PIN, True)
        time.sleep(0.5)
        subprocess.run(
            [
                "rpicam-still",
                "--autofocus-mode", "manual",
                "--lens-position", "22",
                "--rotation", "180",
                "--width", "2304",
                "--height", "1296",
                "--quality", "80",
                "-o", path,
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        GPIO.output(LED_PIN, False)
        GPIO.cleanup()


# ---------------------------------------------------------------------------
# Statefile
# ---------------------------------------------------------------------------

def _load_state():
    """Lädt das Statefile. Gibt None zurück wenn nicht vorhanden."""
    if not os.path.isfile(STATEFILE_PATH):
        return None
    with open(STATEFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    """Schreibt den State atomar: temp-Datei → rename."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(STATEFILE_PATH), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.rename(tmp, STATEFILE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _archive_window(window: dict) -> None:
    """Hängt ein abgeschlossenes Fenster als JSON-Zeile ans Archiv."""
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(window, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Frame bauen
# ---------------------------------------------------------------------------

def _make_frame(now: datetime, reads: dict) -> dict:
    """
    Erzeugt einen Frame-Datensatz gemäß Statefile-Vertrag.

    reads : {boxname: (zeichen, konfidenz)} wie von read_boxes() geliefert
    """
    return {
        "t": _fmt(now),
        "reads": {
            name: [char, conf]
            for name, (char, conf) in reads.items()
        },
    }


# ---------------------------------------------------------------------------
# Kern
# ---------------------------------------------------------------------------

def tick() -> None:
    now = datetime.now(timezone.utc)
    current_quarter_ts = _fmt(_floor_to_quarter(now))

    # ── State laden ──────────────────────────────────────────────────────
    state = _load_state()

    # ── Uhr rückwärts? (NTP-Sprung o.ä.) → Frame verwerfen ──────────────
    if state and state.get("frames"):
        last_t = state["frames"][-1]["t"]
        last_dt = _parse(last_t)
        if now <= last_dt:
            log.warning(
                "Uhr nicht vorwärts (jetzt=%s, letzter Frame=%s) – Frame verworfen.",
                _fmt(now), last_t,
            )
            return

    # ── Foto aufnehmen + OCR ─────────────────────────────────────────────
    tmp = tempfile.NamedTemporaryFile(
        suffix=".jpg", dir=tempfile.gettempdir(), delete=False
    )
    tmp.close()
    foto_path = tmp.name

    try:
        _take_photo(foto_path)
        reads = read_boxes(foto_path)
        log.info(
            "Frame %s: %s",
            _fmt(now),
            {k: v for k, v in reads.items()},
        )
    except Exception as e:
        log.error("Aufnahme oder OCR fehlgeschlagen: %s", e)
        return
    finally:
        try:
            os.unlink(foto_path)
        except OSError:
            pass

    frame = _make_frame(now, reads)

    # ── Fenster-Logik ────────────────────────────────────────────────────

    # Fall 1: Kein Statefile → erstes Fenster starten
    if state is None:
        state = {"window_start": current_quarter_ts, "frames": [frame]}
        _save_state(state)
        log.info("Erstes Fenster gestartet: %s", current_quarter_ts)
        return

    # Fall 2: Gleiche Viertelstunde → Frame anhängen
    if state["window_start"] == current_quarter_ts:
        state["frames"].append(frame)
        _save_state(state)
        log.info(
            "Frame %d ins Fenster %s",
            len(state["frames"]), current_quarter_ts,
        )
        return

    # Fall 3: Neue Viertelstunde → altes Fenster archivieren, neues starten
    log.info(
        "Fenster schließen: %s (%d Frames)",
        state["window_start"], len(state["frames"]),
    )
    _archive_window(state)

    new_state = {"window_start": current_quarter_ts, "frames": [frame]}
    _save_state(new_state)
    log.info("Neues Fenster gestartet: %s", current_quarter_ts)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lock = _acquire_lock()
    if lock is None:
        log.info("Lock belegt – vorheriger Tick läuft noch. Beende.")
        sys.exit(0)

    try:
        tick()
    except Exception as e:
        log.exception("Unerwarteter Fehler in tick(): %s", e)
        sys.exit(1)
    finally:
        lock.close()
