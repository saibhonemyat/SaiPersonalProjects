"""Summarise results/level4_matches.csv: wins and average margin."""
import csv
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "results/level4_matches.csv")
rows = list(csv.DictReader(path.open()))
print(f"{len(rows)} matches in {path}")
for row in rows:
    print("  " + ", ".join(f"{k}={v}" for k, v in row.items()))
