#!/usr/bin/env python3
import os
import json
from datetime import datetime
from flask import Flask, render_template, jsonify

from config import DATA_REPO_DIR, INITIAL_VALUE

DATA_DIR = os.path.join(DATA_REPO_DIR, "data")

FOLDER_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

app = Flask(__name__)


def parse_folder_timestamp(value):
    return datetime.strptime(value, FOLDER_TIMESTAMP_FORMAT + "%z")


def scan_data():
    plausible_entries = []
    implausible_timestamps = []

    if not os.path.isdir(DATA_DIR):
        return plausible_entries, implausible_timestamps

    for entry_name in sorted(os.listdir(DATA_DIR)):
        try:
            parse_folder_timestamp(entry_name)
        except ValueError:
            continue

        entry_file = os.path.join(DATA_DIR, entry_name, "entry.json")
        if not os.path.isfile(entry_file):
            continue

        with open(entry_file) as f:
            data = json.load(f)

        timestamp = data.get("timestamp")
        value_raw = data.get("value_raw", "")

        if not timestamp:
            continue

        if data.get("plausible") and value_raw.isdigit():
            plausible_entries.append({
                "timestamp": timestamp,
                "value": int(value_raw),
            })
        else:
            implausible_timestamps.append(timestamp)

    return plausible_entries, implausible_timestamps


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data/absolute")
def api_absolute():
    entries, _ = scan_data()
    return jsonify(entries)


@app.route("/api/data/implausible")
def api_implausible():
    _, implausible_timestamps = scan_data()
    return jsonify(implausible_timestamps)


@app.route("/api/data/stats")
def api_stats():
    entries, _ = scan_data()

    if not entries:
        return jsonify({"total_since_start": None})

    latest_value = entries[-1]["value"]

    try:
        start_value = int(INITIAL_VALUE)
    except (TypeError, ValueError):
        start_value = entries[0]["value"]

    return jsonify({"total_since_start": latest_value - start_value})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
