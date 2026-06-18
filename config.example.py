# Vorlage für config.py
# Kopiere diese Datei zu config.py und trage deine eigenen Werte ein:
#   cp config.example.py config.py
# config.py selbst wird nicht committed (siehe .gitignore).

# Absoluter Pfad zum lokalen Klon des Daten-Repos (z.B. stromzaehler-daten)
DATA_REPO_DIR = "/home/pi/stromzaehler-daten"

# Manuell abgelesener Startwert, falls noch keine (plausible) Messung existiert
# oder die letzte plausible Messung außerhalb von SEARCH_DAYS_FOR_PLAUSIBLE_DATA liegt.
# Format wie TIMESTAMP_FORMAT in capture.py: "%Y-%m-%dT%H-%M-%S"
INITIAL_VALUE = "000000"
INITIAL_VALUE_TIMESTAMP = "2026-01-01T00:00:00Z"

# Maximal plausibler Anstieg in kWh pro vollen 15 Minuten (wird proportional
# auf die tatsächlich verstrichene Zeit seit der letzten plausiblen Messung skaliert)
MAX_PLAUSIBLE_INCREASE_PER_15_MIN = 5

# Wie viele Tage rückwirkend nach der letzten plausiblen Messung gesucht wird.
# Begrenzt zugleich die Anzahl der zu lesenden Dateien (Performance).
# Wird in diesem Zeitraum keine plausible Messung gefunden (und ist kein
# INITIAL_VALUE konfiguriert), wird nur noch die 6-stellige Ziffernform geprüft.
SEARCH_DAYS_FOR_PLAUSIBLE_DATA = 7
