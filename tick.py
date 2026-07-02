#!/usr/bin/env python3
"""
Minutentakt-Sammler für den Stromzähler.

Aufruf via cron:
  1-14,16-29,31-44,46-59 * * * *  /usr/bin/python3 /home/jonny/stromzaehler-pi/tick.py

Pro Lauf (__main__):
  1. Exklusiven Lock holen (bei Konkurrenz: still beenden)
  2. tick(keep_photo=False) rufen
  3. Lock freigeben

tick(keep_photo):
  1. Foto aufnehmen (IR-LED an/aus, rpicam-still)
  2. Alle 7 Boxen lesen (read_boxes aus boxen.py)
  3. Frame bauen und an Ring-Puffer anhängen (cap RING_BUFFER_SIZE, atomar)
  4. Foto verwerfen (keep_photo=False) oder am festen Pfad liegen lassen (True)

Nichts wird committed. Kein entry.json. Das ist consolidate()-Aufgabe.
Lock liegt im Aufrufer – tick() selbst greift ihn nie (flock ist nicht reentrant,
consolidate hält ihn schon, wenn es tick() ruft).

Dateipfade (alle gitignored, aus config.py):
  RING_BUFFER_PATH   – aktiver Frame-Puffer (JSON)
  KEEP_PHOTO_PATH    – Grenzfoto für consolidate
  RAW_FRAMES_LOG_PATH – Rohframe-JSONL (rotierend)
"""
import fcntl
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import RPi.GPIO as GPIO

from boxen import read_boxes
from config import (
    DATA_REPO_DIR,
    RING_BUFFER_PATH,
    RING_BUFFER_SIZE,
    KEEP_PHOTO_PATH,
    RAW_FRAMES_LOG_PATH,
    DEBUG_BOX_CROPS,
    DEBUG_BOX_CROPS_DIR,
)

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
LED_PIN_01    = 17
LED_PIN_02    = 27
LOCK_PATH  = os.path.join(DATA_REPO_DIR, "tick.lock")
_TS_FMT    = "%Y-%m-%dT%H:%M:%S"   # ohne Z (wird manuell angehängt)
_TS_BASIC  = "%Y%m%dT%H%M%S"       # Basic-Format für Dateinamen/Labels

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt=_TS_FMT,
)
log = logging.getLogger(__name__)

# Zweiter Logger: Rohframes als JSONL, größenbegrenzt, getrennt vom INFO-Log.
# backupCount=1: aktive Datei + eine rotierte ≈ 1 MB Deckel.
_frame_log = logging.getLogger("raw_frames")
_frame_log.propagate = False
_frame_handler = logging.handlers.RotatingFileHandler(
    RAW_FRAMES_LOG_PATH,
    maxBytes=500 * 1024,
    backupCount=1,
    encoding="utf-8",
)
_frame_handler.setFormatter(logging.Formatter("%(message)s"))
_frame_log.addHandler(_frame_handler)
_frame_log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Timestamp-Helfer
# ---------------------------------------------------------------------------

def _fmt(dt: datetime) -> str:
    """datetime → '2026-06-23T18:01:07Z'"""
    return dt.strftime(_TS_FMT) + "Z"


def _fmt_basic(dt: datetime) -> str:
    """datetime → '20260623T180107Z'  (Basic-Format, für Dateinamen/Labels)"""
    return dt.strftime(_TS_BASIC) + "Z"


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
    GPIO.setup(LED_PIN_01, GPIO.OUT)
    GPIO.setup(LED_PIN_02, GPIO.OUT)
    try:
        GPIO.output(LED_PIN_01, True)
        GPIO.output(LED_PIN_02, True)
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
        GPIO.output(LED_PIN_01, False)
        GPIO.output(LED_PIN_02, False)
        GPIO.cleanup()


# ---------------------------------------------------------------------------
# Ring-Puffer
# ---------------------------------------------------------------------------

def _load_buffer() -> dict:
    """Lädt den Ring-Puffer. Gibt leere Struktur zurück wenn nicht vorhanden."""
    if not os.path.isfile(RING_BUFFER_PATH):
        return {"frames": []}
    with open(RING_BUFFER_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_buffer(buf: dict) -> None:
    """Schreibt den Puffer atomar: temp-Datei → rename."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(RING_BUFFER_PATH), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(buf, f)
        os.rename(tmp, RING_BUFFER_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Frame bauen
# ---------------------------------------------------------------------------

def _make_frame(now: datetime, reads: dict) -> dict:
    """
    Erzeugt einen Frame-Datensatz.

    reads : {boxname: (zeichen, konfidenz)} wie von read_boxes() geliefert
    t ist Diagnose-Label/Join-Key, kein Logik-Treiber.
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

def tick(keep_photo: bool = False) -> None:
    """
    Nimmt ein Foto auf, liest alle Boxen und hängt den Frame an den Ring-Puffer.

    keep_photo=False : Foto wird nach dem Aufnehmen gelöscht (Minutenlauf).
    keep_photo=True  : Foto bleibt an KEEP_PHOTO_PATH liegen (Grenzlauf,
                       consolidate übernimmt es als foto.jpg im Entry-Ordner).

    Lock muss vom Aufrufer gehalten werden – tick() greift ihn nie selbst.
    """
    now = datetime.now(timezone.utc)
    debug_label = _fmt_basic(now) if DEBUG_BOX_CROPS else None

    # ── Foto aufnehmen ────────────────────────────────────────────────────
    if keep_photo:
        foto_path = KEEP_PHOTO_PATH
        cleanup_foto = False
    else:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg", dir=tempfile.gettempdir(), delete=False
        )
        tmp.close()
        foto_path = tmp.name
        cleanup_foto = True

    try:
        _take_photo(foto_path)
        reads = read_boxes(
            foto_path,
            debug_label=debug_label,
            debug_dir=DEBUG_BOX_CROPS_DIR if DEBUG_BOX_CROPS else None,
        )
        log.info("Frame %s: %s", _fmt(now), {k: v for k, v in reads.items()})
    except Exception as e:
        log.error("Aufnahme oder OCR fehlgeschlagen: %s", e)
        if cleanup_foto:
            try:
                os.unlink(foto_path)
            except OSError:
                pass
        return
    finally:
        if cleanup_foto:
            try:
                os.unlink(foto_path)
            except OSError:
                pass

    frame = _make_frame(now, reads)

    # ── Rohframe loggen ───────────────────────────────────────────────────
    _frame_log.info(json.dumps(frame, ensure_ascii=False))

    # ── Ring-Puffer aktualisieren ─────────────────────────────────────────
    buf = _load_buffer()
    buf["frames"].append(frame)
    # Älteste Frames abschneiden, wenn Cap überschritten
    if len(buf["frames"]) > RING_BUFFER_SIZE:
        buf["frames"] = buf["frames"][-RING_BUFFER_SIZE:]
    _save_buffer(buf)

    log.info(
        "Frame an Puffer gehängt (%d/%d). keep_photo=%s",
        len(buf["frames"]), RING_BUFFER_SIZE, keep_photo,
    )


# ---------------------------------------------------------------------------
# Einstiegspunkt (Minutenlauf)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lock = _acquire_lock()
    if lock is None:
        log.info("Lock belegt – vorheriger Tick läuft noch. Beende.")
        sys.exit(0)

    try:
        tick(keep_photo=False)
    except Exception as e:
        log.exception("Unerwarteter Fehler in tick(): %s", e)
        sys.exit(1)
    finally:
        lock.close()
