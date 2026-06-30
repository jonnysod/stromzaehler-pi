#!/usr/bin/env python3
"""
Viertelstunden-Konsolidierung: aus dem Ring-Puffer einen autorisierten
Wh-Integer + Foto + entry.json + commit/push erzeugen.

Implementiert den korrigierten Algorithmus aus plan-consolidate-ergaenzung.md
(ersetzt die ursprüngliche D2/D5-Klassifikation und Hash-Chain vollständig):

  - Carry bestimmt die Block/Tail-Grenze, nicht eine Mehrheits-Klassifikation
    vorab (Abschnitt 1). Der Tail startet immer zweistellig [d6, Z], weil ein
    Z-Überlauf nur gemeinsam mit d6 beurteilbar ist - als gemeinsame Zahl
    behandelt, erzwingt das automatisch den "joint mit d6"-Check.
  - Kipp-Regel / Treppe (Abschnitt 2): legale Nachfolger hängen sich sofort
    ein; Widersprüche brauchen drei konsekutive, monotone Bestätiger, um den
    Anker rückwirkend zu kippen. Schützt gegen den +1-Ratchet-Fehler.
  - Schritt-Limit pro Frame aus MAX_POWER_W, nicht aus dem 15-Minuten-Limit
    (Abschnitt 3).
  - Leeres Z am Grenzframe: einstufige Rekonstruktion über gevotetes D6
    gegen den letzten "both-good"-Anker (Abschnitt 5).
  - Hash-Modell B1 (Abschnitt 6): kein entry_hash/prev_hash mehr, Git trägt
    die Integrität. Nur image_hash bleibt als unveränderlicher Anker.
  - Jüngster Eintrag ist überschreibbar (Abschnitt 7): der Ring-Puffer
    spannt über zwei Fenster, sodass ein Grenz-Spike im nächsten Lauf durch
    inzwischen vorliegende Folgeframes korrigiert werden kann.

Aufruf via cron:
  0,15,30,45 * * * *  /usr/bin/python3 /home/jonny/stromzaehler-pi/consolidate.py
"""
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from tick import _acquire_lock, tick
from config import (
    DATA_REPO_DIR,
    RING_BUFFER_PATH,
    CONSOLIDATE_LOOKBACK,
    KIP_MIN_CONFIDENCE,
    KIP_THRESHOLD,
    MAX_POWER_W,
    KEEP_PHOTO_PATH,
    INITIAL_VALUE,
    INITIAL_VALUE_TIMESTAMP,
    MAX_PLAUSIBLE_INCREASE_PER_15_MIN,
    SEARCH_DAYS_FOR_PLAUSIBLE_DATA,
)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
_TS_FMT = "%Y-%m-%dT%H:%M:%S"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt=_TS_FMT,
)
log = logging.getLogger(__name__)

DATA_DIR = os.path.join(DATA_REPO_DIR, "data")

# Timestamp-Formate (konsistent mit tick.py)
_FOLDER_FMT   = "%Y%m%dT%H%M%S"        # Ordnername
_ENTRY_FMT    = "%Y-%m-%dT%H:%M:%S"    # entry.json "timestamp"
_FRAME_T_FMT  = "%Y-%m-%dT%H:%M:%S"    # Frame "t" (wie tick.py schreibt)

# Stellen-Reihenfolge, wie sie in den Frames vorliegt (links -> rechts)
BOX_NAMES = ["d1", "d2", "d3", "d4", "d5", "d6", "Z"]

# Reihenfolge, in der der Tail nach links erweitert wird, falls der Carry
# über eine zweistellige [d6,Z]-Trajektorie hinausläuft (Kaskaden-Fall).
TAIL_WIDEN_ORDER = ["d5", "d4", "d3", "d2", "d1"]


# ---------------------------------------------------------------------------
# Timestamp-Helfer
# ---------------------------------------------------------------------------

def _fmt_folder(dt: datetime) -> str:
    return dt.strftime(_FOLDER_FMT) + "Z"


def _fmt_entry(dt: datetime) -> str:
    return dt.strftime(_ENTRY_FMT) + "Z"


def _parse_entry(s: str) -> datetime:
    return datetime.strptime(s, _ENTRY_FMT + "%z")


def _parse_frame_t(s: str) -> datetime:
    return datetime.strptime(s, _FRAME_T_FMT + "%z")


def _floor_to_quarter(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Puffer lesen
# ---------------------------------------------------------------------------

def _load_frames() -> list:
    """Lädt den vollständigen Ring-Puffer (bis RING_BUFFER_SIZE Frames,
    spannt über ~2 Fenster - wichtig für die Überschreib-Logik in Abschnitt 7)."""
    if not os.path.isfile(RING_BUFFER_PATH):
        return []
    with open(RING_BUFFER_PATH, encoding="utf-8") as f:
        return json.load(f).get("frames", [])


# ---------------------------------------------------------------------------
# Konfidenzgewichtete Mehrheit (für Block-Stellen)
# ---------------------------------------------------------------------------

def _weighted_vote(readings: list) -> tuple:
    """
    readings: Liste von (zeichen, konfidenz). Leere Zeichen werden ignoriert.
    Gibt (gewinner_zeichen, gesamtgewicht) zurück, ("", 0) falls nichts Gültiges da ist.
    """
    weights = {}
    for char, conf in readings:
        if char:
            weights[char] = weights.get(char, 0) + max(conf, 1)
    if not weights:
        return "", 0
    winner = max(weights, key=lambda c: weights[c])
    return winner, weights[winner]


def _vote_block(frames: list, block_names: list) -> dict:
    """Für jede Block-Stelle konfidenzgewichtete Mehrheit über alle Frames."""
    result = {}
    for name in block_names:
        readings = [frame["reads"].get(name, ("", 0)) for frame in frames]
        winner, _ = _weighted_vote(readings)
        result[name] = winner
    return result


# ---------------------------------------------------------------------------
# Schritt-Limit (Abschnitt 3): Magnitude-Bound aus MAX_POWER_W, nicht Ordnung
# ---------------------------------------------------------------------------

def _digit_step_limit(elapsed_min: float, max_power_w: int) -> int:
    """
    ceil(MAX_POWER_W * elapsed_min / 6000), mit 1-Minuten-Floor.
    Herleitung: 10000 W / 60 = 167 Wh/min, / 100 Wh pro Z-Schritt ≈ 1,67
    -> aufgerundet 2 Z-Schritte/min bei MAX_POWER_W=10000 (6000 = 60*100).
    Der Floor ist der Guard gegen Δt<=0 bei NTP-Rückwärtssprüngen - die
    Reihenfolge bleibt davon unberührt, nur wie weit ein Schritt springen darf.
    """
    elapsed_min = max(elapsed_min, 1)
    return max(1, math.ceil(max_power_w * elapsed_min / 6000))


# ---------------------------------------------------------------------------
# Tail-Wert pro Frame
# ---------------------------------------------------------------------------

def _frame_tail_value(frame: dict, tail_names: list):
    """
    Setzt die Tail-Stellen eines Frames zu einer Zahl zusammen.
    Gibt (wert, min_konfidenz) zurück, oder (None, None) wenn irgendeine
    Tail-Stelle in diesem Frame leer oder nicht-numerisch ist (Tor 1).
    """
    chars, confs = [], []
    for name in tail_names:
        char, conf = frame["reads"].get(name, ("", 0))
        if not char or not char.isdigit():
            return None, None
        chars.append(char)
        confs.append(conf)
    return int("".join(chars)), min(confs)


# ---------------------------------------------------------------------------
# Kipp-Regel: revidierbare Treppe über die Tail-Trajektorie (Abschnitt 2)
# ---------------------------------------------------------------------------

def _candidates_form_staircase(candidates: list, period: int, max_power_w: int) -> bool:
    """Prüft ob die gesammelten Widerspruchs-Kandidaten untereinander selbst
    eine konsistente, monotone Mini-Treppe bilden (sonst: Rauschen, kippt nichts)."""
    prev_val, prev_t = None, None
    for _idx, val, _conf, t in candidates:
        if prev_val is not None:
            elapsed_min = (t - prev_t).total_seconds() / 60
            limit = _digit_step_limit(elapsed_min, max_power_w)
            diff = (val - prev_val) % period
            if diff > limit:
                return False
        prev_val, prev_t = val, t
    return True


def _find_rollback_base(history: list, first_candidate: tuple, period: int,
                         max_power_w: int):
    """
    Sucht rückwärts in der Mount-Historie den jüngsten Punkt, von dem aus der
    erste Kandidat ein legaler Vorwärtsschritt wäre. Das ist der Punkt, auf
    den der Anker zurückgesetzt wird - alles danach (der Spike) war der Irrtum
    und wird verworfen, statt fälschlich als riesiger Vorwärts-Wrap vom Spike
    aus weitergerechnet zu werden.
    """
    _cidx, cval, _cconf, ct = first_candidate
    for pos in range(len(history) - 1, -1, -1):
        _idx, val, _abs, t = history[pos]
        elapsed_min = (ct - t).total_seconds() / 60
        if elapsed_min <= 0:
            continue
        limit = _digit_step_limit(elapsed_min, max_power_w)
        diff = (cval - val) % period
        if diff <= limit:
            return pos
    return None


def _unroll_tail_staircase(frames: list, tail_names: list,
                            kip_min_conf: int, kip_threshold: int,
                            max_power_w: int) -> list:
    """
    Baut die Treppe über die Tail-Trajektorie (mod 10^len(tail_names)).

    Frames werden in Append-Reihenfolge durchlaufen (Index 0 -> Ende = physische
    Aufnahmereihenfolge, nicht nach 't' sortiert - siehe Ergänzung Abschnitt 4).

    Pro Frame, zwei Tore:
      Tor 1 "erkannt": Tail-Stellen vorhanden, conf >= kip_min_conf. Sonst
        -> Frame übersprungen (zählt nirgends mit).
      Tor 2 "eingehängt":
        Fall A (legaler Nachfolger, Schritt <= Limit, mod-Wrap inklusive)
          -> sofort eingehängt, Kandidatenliste geleert.
        Fall B (Widerspruch) -> als Kandidat gesammelt. Erst wenn die letzten
          kip_threshold Kandidaten selbst eine konsistente, monotone
          Mini-Treppe bilden, wird die Mount-Historie auf den letzten dazu
          passenden Punkt ZURÜCKGESETZT (der zwischenzeitliche Spike fällt
          komplett raus, statt als Vorwärts-Wrap mitgerechnet zu werden) und
          die Kandidaten werden ab dort neu eingehängt.

    Der Anker für den jeweils nächsten Vergleich ist immer der letzte
    EINGEHÄNGTE Wert (= Ende der Mount-Historie), nie der letzte bloß erkannte.

    Rückgabe: mounted = [(frame_idx, absoluter_wert), ...], aufsteigend nach idx.
    absoluter_wert ist NICHT auf 'period' begrenzt - Werte >= period zeigen an,
    dass der Carry über diese Tail-Breite hinausläuft (s. _determine_tail).
    """
    period = 10 ** len(tail_names)
    history = []  # [(frame_idx, value_mod_period, absoluter_wert, t), ...]
    candidates = []

    for idx, frame in enumerate(frames):
        value, conf = _frame_tail_value(frame, tail_names)
        if value is None or conf < kip_min_conf:
            continue  # Tor 1 nicht passiert

        t = _parse_frame_t(frame["t"])

        if not history:
            # Erster solider Read = Saat, wird gesetzt, nicht geprüft.
            history.append((idx, value, value, t))
            candidates = []
            continue

        a_idx, a_val, a_abs, a_t = history[-1]
        elapsed_min = (t - a_t).total_seconds() / 60
        limit = _digit_step_limit(elapsed_min, max_power_w)
        diff = (value - a_val) % period

        if diff <= limit:
            # Fall A: legaler Nachfolger (auch bei Wrap, z.B. 9->0 mit diff=1
            # weil d6 Teil der gemeinsamen Zahl ist - "joint mit d6" automatisch
            # erzwungen, sonst wäre diff riesig und würde unten landen).
            history.append((idx, value, a_abs + diff, t))
            candidates = []
        else:
            # Fall B: Widerspruch -> Kandidat sammeln, Historie bleibt vorerst stehen.
            candidates.append((idx, value, conf, t))
            if len(candidates) >= kip_threshold:
                recent = candidates[-kip_threshold:]
                if _candidates_form_staircase(recent, period, max_power_w):
                    base_pos = _find_rollback_base(history, recent[0], period, max_power_w)
                    if base_pos is not None:
                        history = history[:base_pos + 1]
                        for cidx, cval, _cconf, ct in recent:
                            _li, lv, la, lt = history[-1]
                            cdiff = (cval - lv) % period
                            history.append((cidx, cval, la + cdiff, ct))
                    candidates = []

    return [(idx, abs_val) for idx, _val, abs_val, _t in history]


def _determine_tail(frames: list, kip_min_conf: int, kip_threshold: int,
                     max_power_w: int):
    """
    Findet die minimale Tail-Breite, die den beobachteten Carry vollständig
    trägt (Abschnitt 1). Startet immer zweistellig [d6, Z] - Z lässt sich nie
    allein validieren, ein Überlauf braucht d6 als Mitleser. Erweitert
    iterativ nach links (d5, d4, ...), solange der Carry über die aktuelle
    Breite hinausläuft.

    Rückgabe: (tail_names, mounted, period) für die final passende Breite.
    """
    tail_names = ["d6", "Z"]
    widen_pool = list(TAIL_WIDEN_ORDER)

    while True:
        period = 10 ** len(tail_names)
        mounted = _unroll_tail_staircase(
            frames, tail_names, kip_min_conf, kip_threshold, max_power_w
        )
        if not mounted:
            return tail_names, mounted, period  # kein solider Read überhaupt

        carry = mounted[-1][1] // period
        if carry == 0 or not widen_pool:
            return tail_names, mounted, period

        log.info(
            "Carry läuft über Tail %s hinaus (carry=%d) – erweitere nach links.",
            tail_names, carry,
        )
        tail_names = [widen_pool.pop(0)] + tail_names


# ---------------------------------------------------------------------------
# Leeres Z am Grenzframe: einstufige Rekonstruktion (Abschnitt 5)
# ---------------------------------------------------------------------------

def _local_digit_vote(frames: list, name: str, end_idx: int, lookback: int,
                       kip_min_conf: int) -> tuple:
    """Konfidenzgewichtete Mehrheit für 'name' über die letzten `lookback`
    Frames bis end_idx (inklusive). Für 'D6_grenz' wird lookback=1 verwendet
    (nur der Grenzframe selbst zählt, gegated durch Konfidenz) - ein größeres
    Fenster würde mit älteren Frames verwässern, falls d6 genau am
    Grenzframe tickt (alter Wert gewinnt fälschlich die Mehrheit) oder
    fälschlich auffüllen, wenn der Grenzframe selbst leer ist."""
    start = max(0, end_idx - lookback + 1)
    readings = []
    for frame in frames[start:end_idx + 1]:
        char, conf = frame["reads"].get(name, ("", 0))
        if conf >= kip_min_conf:
            readings.append((char, conf))
    return _weighted_vote(readings)


def _reconstruct_grenz(frames: list, tail_names: list, mounted: list,
                        kip_min_conf: int):
    """
    Einstufige Rekonstruktion: Grenzframe hat kein solides Z, aber evtl. ein
    solides D6. Anker = letztes Frame, in dem D6 UND Z solide im selben Bild
    gelesen wurden (= letzter mounted-Punkt, da Mounten genau das voraussetzt).

    D6_grenz == D6_letzter_Anker     -> Z = Z_letzter_Anker  (untere Schranke)
    D6_grenz == D6_letzter_Anker + 1 -> Z = 0                (Überlauf gerade passiert)
    sonst                            -> nicht rekonstruierbar

    Nur für den Standardfall implementiert, in dem der Tail genau [..., d6, Z]
    ist (immer der Fall, solange kein Kaskaden-Carry weiter nach links lief).
    Bei einer Kaskade wird hier bewusst NICHT tiefer geraten - lieber eine
    ehrliche Lücke als eine zweite, gestapelte Annahme (s. Abschnitt 5: die
    Tiefe der Rekonstruktion ist auf eine Stufe begrenzt, anders als die
    arithmetische Carry-Propagation in _determine_tail).
    """
    if not mounted or len(tail_names) < 2 or tail_names[-2:] != ["d6", "Z"]:
        return None, None

    grenz_idx = len(frames) - 1
    _last_idx, last_abs = mounted[-1]
    period = 10 ** len(tail_names)

    d6_l = (last_abs % period) // 10
    z_l = last_abs % 10

    d6_grenz, _conf = _local_digit_vote(
        frames, "d6", grenz_idx, lookback=1, kip_min_conf=kip_min_conf
    )
    if not d6_grenz or not d6_grenz.isdigit():
        return None, None

    d6_grenz_val = int(d6_grenz)

    if d6_grenz_val == d6_l:
        z_reconstructed = z_l
    elif d6_grenz_val == (d6_l + 1) % 10:
        z_reconstructed = 0
    else:
        return None, None  # weder stabil noch genau +1 -> nicht rekonstruierbar

    # Stellen oberhalb von d6 (falls Tail breiter als 2) unverändert vom
    # letzten Anker übernehmen - sie waren dort laut Carry=0 nachweislich stabil.
    upper_part = (last_abs % period) // 100
    tail_val = upper_part * 100 + d6_grenz_val * 10 + z_reconstructed
    tail_str = str(tail_val).zfill(len(tail_names))
    return tail_str, "z_reconstructed_from_d6"


def _resolve_grenz_tail(frames: list, tail_names: list, mounted: list,
                         kip_min_conf: int):
    """
    Tail-Wert für den Grenzframe (= frames[-1]).
    Bevorzugt: Grenzframe wurde selbst in der Treppe eingehängt -> direkt verwenden.
    Fallback: einstufige D6-Rekonstruktion.
    Sonst: (None, None) -> kein Entry.
    """
    if not mounted:
        return None, None

    grenz_idx = len(frames) - 1
    last_idx, last_abs = mounted[-1]
    period = 10 ** len(tail_names)

    if last_idx == grenz_idx:
        return str(last_abs % period).zfill(len(tail_names)), None

    return _reconstruct_grenz(frames, tail_names, mounted, kip_min_conf)


# ---------------------------------------------------------------------------
# Wert zusammensetzen
# ---------------------------------------------------------------------------

def _compose_wh(block: dict, block_names: list, tail_str: str):
    block_str = "".join(block.get(n, "") for n in block_names)
    seven_str = block_str + tail_str
    if len(seven_str) != 7 or not seven_str.isdigit():
        return None
    return int(seven_str) * 100


# ---------------------------------------------------------------------------
# Plausibilität (unverändert in der Logik, nur in Wh)
# ---------------------------------------------------------------------------

def _get_previous_entry():
    """Jüngsten vorhandenen entry.json finden.
    Rückgabe: (ordnername, entry_dict) oder (None, None).
    Muss vor os.makedirs() für den neuen Entry-Ordner aufgerufen werden,
    sonst findet sie den eigenen, gerade erst angelegten leeren Ordner."""
    if not os.path.isdir(DATA_DIR):
        return None, None
    for name in sorted(os.listdir(DATA_DIR), reverse=True):
        path = os.path.join(DATA_DIR, name, "entry.json")
        if os.path.isfile(path):
            with open(path) as f:
                return name, json.load(f)
    return None, None


def _get_last_plausible_entry():
    """Forward-Pass über alle Entries; gibt letzten plausiblen zurück
    (ignoriert das gespeicherte 'plausible'-Feld, wertet nur value_raw/timestamp)."""
    if INITIAL_VALUE:
        last = {"value_raw": INITIAL_VALUE, "timestamp": INITIAL_VALUE_TIMESTAMP}
    else:
        last = None

    if not os.path.isdir(DATA_DIR):
        return last

    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_DAYS_FOR_PLAUSIBLE_DATA)
    for name in sorted(os.listdir(DATA_DIR)):
        try:
            dt = datetime.strptime(name, _FOLDER_FMT + "%z")
        except ValueError:
            continue
        if dt < cutoff:
            continue
        path = os.path.join(DATA_DIR, name, "entry.json")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            data = json.load(f)
        if _check_plausibility(data.get("value_raw"), data.get("timestamp"), last):
            last = data
    return last


def _check_plausibility(value_raw, current_ts, previous_entry) -> bool:
    """Prüft ob value_raw (Wh-Integer) gegen den Vorgänger plausibel ist."""
    if not isinstance(value_raw, int):
        return False
    if previous_entry is None:
        return True

    prev_raw = previous_entry.get("value_raw")
    if not isinstance(prev_raw, int):
        return True  # Vorgänger selbst nicht verlässlich, kein Vergleich möglich

    try:
        prev_time = _parse_entry(previous_entry["timestamp"])
        curr_time = _parse_entry(current_ts)
    except (TypeError, ValueError, KeyError):
        return True

    elapsed_minutes = max((curr_time - prev_time).total_seconds() / 60, 1)
    max_allowed = MAX_PLAUSIBLE_INCREASE_PER_15_MIN * (elapsed_minutes / 15)
    diff = value_raw - prev_raw
    return 0 <= diff <= max_allowed


# ---------------------------------------------------------------------------
# Schreiben (Hash-Modell B1: kein entry_hash/prev_hash mehr, s. Abschnitt 6)
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _write_entry(entry_dir: str, timestamp_str: str, value_wh: int,
                  plausible: bool, note: str = None) -> str:
    """Schreibt entry.json + verschiebt das Grenzfoto. Git trägt die
    Integrität, daher kein entry_hash/prev_hash mehr - nur image_hash als
    unveränderlicher Anker am Foto."""
    foto_dst = os.path.join(entry_dir, "foto.jpg")
    os.replace(KEEP_PHOTO_PATH, foto_dst)
    image_hash = _sha256_file(foto_dst)

    entry = {
        "timestamp": timestamp_str,
        "value_raw": value_wh,
        "plausible": plausible,
        "image_hash": image_hash,
    }
    if note:
        entry["note"] = note

    entry_file = os.path.join(entry_dir, "entry.json")
    with open(entry_file, "w") as f:
        json.dump(entry, f, indent=2)
    return entry_file


# ---------------------------------------------------------------------------
# Vorherigen Entry ggf. überschreiben (Abschnitt 7)
# ---------------------------------------------------------------------------

def _frame_idx_nearest(frames: list, target_dt: datetime):
    """Index des Frames, dessen 't' am dichtesten an target_dt liegt."""
    best_idx, best_diff = None, None
    for idx, frame in enumerate(frames):
        t = _parse_frame_t(frame["t"])
        diff = abs((t - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best_idx, best_diff = idx, diff
    return best_idx


def _resolve_tail_at(tail_names: list, mounted: list, target_idx: int):
    """Tail-Wert am letzten mounted-Punkt mit idx <= target_idx ablesen.
    'mounted' ist nach dem vollständigen Walk final - auch rückwirkend
    gekippte Punkte stecken schon korrekt drin."""
    period = 10 ** len(tail_names)
    candidate = None
    for idx, abs_val in mounted:
        if idx <= target_idx:
            candidate = abs_val
        else:
            break
    if candidate is None:
        return None
    return str(candidate % period).zfill(len(tail_names))


def _maybe_overwrite_previous(frames: list, tail_names: list, mounted: list,
                               block: dict, block_names: list,
                               previous_folder: str, previous_entry: dict):
    """
    Berechnet den unmittelbar vorherigen Entry mit den jetzt verfügbaren
    Folgeframes neu (Ringpuffer spannt über zwei Fenster). Nur value_raw und
    note werden überschrieben; foto.jpg/image_hash bleiben unangetastet -
    das Foto ist und bleibt der unveränderliche Anker, der Wert eine
    revidierbare Annotation. Mit Hash-Modell B1 ist das folgenlos, weil keine
    Entry-Kette bricht; die Git-History selbst ist das Audit-Protokoll.

    Gibt True zurück, wenn tatsächlich überschrieben wurde (für den commit).
    """
    if previous_folder is None or previous_entry is None:
        return False

    try:
        prev_dt = _parse_entry(previous_entry["timestamp"])
    except (KeyError, ValueError):
        return False

    target_idx = _frame_idx_nearest(frames, prev_dt)
    if target_idx is None:
        return False

    tail_str = _resolve_tail_at(tail_names, mounted, target_idx)
    if tail_str is None:
        return False

    new_wh = _compose_wh(block, block_names, tail_str)
    if new_wh is None:
        return False

    old_wh = previous_entry.get("value_raw")
    if new_wh == old_wh:
        return False  # nichts zu korrigieren

    log.info(
        "Vorheriger Entry %s wird korrigiert: %s Wh -> %s Wh",
        previous_entry["timestamp"], old_wh, new_wh,
    )

    entry_file = os.path.join(DATA_DIR, previous_folder, "entry.json")
    if not os.path.isfile(entry_file):
        return False

    updated = dict(previous_entry)
    updated["value_raw"] = new_wh
    updated["note"] = "value_revised"
    # plausible wird hier bewusst nicht neu bewertet (bräuchte den Vorgänger
    # DES Vorgängers) - get_last_plausible_entry() bestimmt das beim nächsten
    # Forward-Pass ohnehin korrekt neu.

    with open(entry_file, "w") as f:
        json.dump(updated, f, indent=2)

    return True


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git_commit_push(timestamp_str: str, entry_dir: str,
                      previous_folder: str = None, previous_overwritten: bool = False):
    subprocess.run(["git", "add", entry_dir], cwd=DATA_REPO_DIR, check=True)
    if previous_overwritten and previous_folder:
        prev_dir = os.path.join(DATA_DIR, previous_folder)
        subprocess.run(["git", "add", prev_dir], cwd=DATA_REPO_DIR, check=True)

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=DATA_REPO_DIR)
    if diff.returncode == 0:
        log.info("Keine Änderungen zu committen.")
        return

    msg = f"feat: measurement {timestamp_str}"
    if previous_overwritten:
        msg += " (+ correction of previous entry)"
    subprocess.run(["git", "commit", "-m", msg], cwd=DATA_REPO_DIR, check=True)
    subprocess.run(["git", "push"], cwd=DATA_REPO_DIR, check=True)


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def consolidate() -> None:
    now = datetime.now(timezone.utc)
    boundary = _floor_to_quarter(now)
    ts_folder = _fmt_folder(boundary)
    ts_entry = _fmt_entry(boundary)

    log.info("consolidate() start – Grenze %s", ts_entry)

    # ── Grenzframe + Grenzfoto erzwingen ─────────────────────────────────
    tick(keep_photo=True)

    # ── Puffer laden (voller Ringpuffer, spannt über ~2 Fenster) ────────
    frames = _load_frames()
    if not frames:
        log.warning("Puffer leer – kein Entry geschrieben.")
        return
    log.info("Puffer: %d Frames verfügbar.", len(frames))

    # ── Carry-first: Tail-Breite + Treppe bestimmen (Abschnitt 1+2) ──────
    tail_names, mounted, _period = _determine_tail(
        frames, KIP_MIN_CONFIDENCE, KIP_THRESHOLD, MAX_POWER_W
    )
    block_names = [n for n in BOX_NAMES if n not in tail_names]
    log.info("Tail: %s  Block: %s", tail_names, block_names)

    if not mounted:
        log.warning("Kein solider Tail-Read im Puffer – kein Entry geschrieben.")
        return

    cutoff_idx = max(0, len(frames) - CONSOLIDATE_LOOKBACK)
    if mounted[-1][0] < cutoff_idx:
        log.warning(
            "Letzter solider Tail-Read liegt außerhalb der letzten %d Frames – kein Entry.",
            CONSOLIDATE_LOOKBACK,
        )
        return

    # ── Block voten ───────────────────────────────────────────────────────
    block = _vote_block(frames, block_names)
    log.info("Block-Vote: %s", block)

    # ── Grenzwert auflösen (direkt oder einstufig rekonstruiert) ─────────
    tail_str, note = _resolve_grenz_tail(frames, tail_names, mounted, KIP_MIN_CONFIDENCE)
    if tail_str is None:
        log.warning("Grenzwert nicht auflösbar (leeres Z, keine passende Rekonstruktion) – kein Entry.")
        return

    wh_int = _compose_wh(block, block_names, tail_str)
    if wh_int is None:
        log.warning("Block-Vote unvollständig – kein Entry geschrieben.")
        return

    log.info(
        "Endstand: %d Wh (%.1f kWh)%s", wh_int, wh_int / 1000,
        f"  note: {note}" if note else "",
    )

    # ── Vorgänger + Plausibilität ─────────────────────────────────────────
    previous_folder, previous_entry = _get_previous_entry()  # vor makedirs()!
    last_plausible_entry = _get_last_plausible_entry()
    plausible = _check_plausibility(wh_int, ts_entry, last_plausible_entry)
    log.info("Plausibilität: %s", plausible)

    # ── Schreiben ─────────────────────────────────────────────────────────
    entry_dir = os.path.join(DATA_DIR, ts_folder)
    os.makedirs(entry_dir, exist_ok=True)

    entry_file = _write_entry(entry_dir, ts_entry, wh_int, plausible, note)
    log.info("Entry geschrieben: %s", entry_file)

    # ── Vorherigen Entry ggf. mit jetzt verfügbaren Folgeframes korrigieren ─
    previous_overwritten = _maybe_overwrite_previous(
        frames, tail_names, mounted, block, block_names,
        previous_folder, previous_entry,
    )

    _git_commit_push(ts_entry, entry_dir, previous_folder, previous_overwritten)
    log.info("Committed und gepusht: %s", ts_entry)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lock = _acquire_lock()
    if lock is None:
        log.info("Lock belegt – consolidate läuft noch. Beende.")
        sys.exit(0)

    try:
        consolidate()
    except Exception as e:
        log.exception("Unerwarteter Fehler in consolidate(): %s", e)
        sys.exit(1)
    finally:
        lock.close()
