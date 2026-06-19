#!/usr/bin/env python3
import os
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request

from config import DATA_REPO_DIR

DATA_DIR = os.path.join(DATA_REPO_DIR, "data")

# Gleiche Formate wie in capture.py (dort auch der Kommentar zu "Z"/%z).
FOLDER_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"
ENTRY_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

app = Flask(__name__)


def parse_folder_timestamp(value):
    return datetime.strptime(value, FOLDER_TIMESTAMP_FORMAT + "%z")


def parse_entry_timestamp(value):
    return datetime.strptime(value, ENTRY_TIMESTAMP_FORMAT + "%z")


def load_entries(from_ts=None, to_ts=None):
    """Liest alle plausiblen Messwerte aus DATA_DIR, optional auf einen
    Zeitraum eingeschränkt.

    Filtert anhand des Ordnernamens, BEVOR entry.json überhaupt geöffnet
    wird - analog zu SEARCH_DAYS_FOR_PLAUSIBLE_DATA in capture.py, wichtig
    für die Performance bei wachsender Datenmenge.
    """
    entries = []

    if not os.path.isdir(DATA_DIR):
        return entries

    for entry_name in sorted(os.listdir(DATA_DIR)):
        try:
            folder_time = parse_folder_timestamp(entry_name)
        except ValueError:
            continue  # kein gültiger Messordner, z.B. .git oder Sonstiges

        if from_ts and folder_time < from_ts:
            continue
        if to_ts and folder_time > to_ts:
            continue

        entry_file = os.path.join(DATA_DIR, entry_name, "entry.json")
        if not os.path.isfile(entry_file):
            continue

        with open(entry_file) as f:
            data = json.load(f)

        if not data.get("plausible"):
            continue  # unplausible Messungen werden ignoriert, nicht interpoliert

        timestamp = data.get("timestamp")
        value_raw = data.get("value_raw", "")
        if not timestamp or not value_raw.isdigit():
            continue

        entries.append({
            "timestamp": timestamp,
            "value": int(value_raw),
        })

    return entries


def parse_query_timestamp(param_name):
    """Liest einen optionalen ?from=/?to= Query-Parameter.
    Erwartetes Format wie in entry.json: 2026-06-17T18:44:32Z
    Wirft ValueError bei vorhandenem, aber ungültigem Wert."""
    raw = request.args.get(param_name)
    if not raw:
        return None
    return parse_entry_timestamp(raw)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    try:
        from_ts = parse_query_timestamp("from")
        to_ts = parse_query_timestamp("to")
    except ValueError:
        return jsonify({
            "error": "from/to müssen im Format YYYY-MM-DDTHH:MM:SSZ vorliegen"
        }), 400

    return jsonify(load_entries(from_ts, to_ts))


if __name__ == "__main__":
    # host="0.0.0.0": Server lauscht auf allen Netzwerk-Interfaces, nicht
    # nur localhost - dadurch erreichbar von anderen Geräten im Heimnetz
    # (z.B. Laptop unter stromzaehler.local:5000). Kein Port-Forwarding
    # zum Internet eingerichtet, bleibt also faktisch Heimnetz-only.
    app.run(host="0.0.0.0", port=5000)
