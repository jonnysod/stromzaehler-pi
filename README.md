# stromzaehler-pi

A Raspberry Pi photographs a mechanical electricity meter and reads it via OCR. Every quarter hour it stores the reading as a Wh integer, together with the photo as evidence, in a (private) data repo. A local dashboard shows consumption.

> **Status:** calibration in progress.

## How it works

```
every minute    tick.py         photo → OCR of 7 digits → frame into ring buffer
every 15 min    consolidate.py  ring buffer → reading → entry.json + photo → git push
```

Instead of relying on a single photo, `consolidate.py` evaluates the **sequence** of recent frames. That way, rolling digits caught mid-transition and occasional misreads don't corrupt the value. Git provides integrity, and each photo is anchored in its entry by its SHA-256 hash.

## Files

| File | Purpose |
|---|---|
| `boxen.py` | Digit box calibration (`BOXES`) and OCR |
| `tick.py` | Per-minute collector |
| `consolidate.py` | Quarter-hour evaluation and commit |
| `dashboard.py`, `templates/index.html` | Flask dashboard |
| `boxen_test.py` | Calibration helper |
| `config.example.py` | Template for `config.py` |

## Setup

Requires a Raspberry Pi with a Camera Module 3 NoIR and an IR LED on GPIO 17.

```bash
sudo apt install git imagemagick tesseract-ocr python3-flask python3-rpi.gpio
cp config.example.py config.py   # set paths and initial reading
```

Crontab:

```
1-14,16-29,31-44,46-59 * * * *  python3 ~/stromzaehler-pi/tick.py
0,15,30,45             * * * *  python3 ~/stromzaehler-pi/consolidate.py
```

Dashboard: run `python3 dashboard.py`, then open `http://<pi>:5000`.

## Calibration

```bash
python3 boxen_test.py photo.jpg --ocr
```

This produces an overlay image and a contact sheet. Adjust `BOXES` in `boxen.py` until every digit sits centered in its box.

## Built with Claude

This project is developed in collaboration with [Claude](https://claude.ai) (Anthropic). Concept, algorithm and code are discussed and written together; decisions and testing on the real meter are mine.
