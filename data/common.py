"""Small shared helpers for dataset preparation (Python 3.10+)."""

import csv
import json
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "butterfly-bbox/1.0 (educational CC0 dataset)"}
CANDIDATE_FIELDS = [
    "photo_id", "observation_id", "observation_url", "image_url",
    "photo_page_url", "license_code", "attribution", "observer_login",
    "source_taxon_id", "observation_taxon", "source_bucket", "proposed_class",
]
DOWNLOAD_FIELDS = ["photo_id", "local_path", "width", "height", "exif_orientation", "status", "error"]
DECISION_FIELDS = ["photo_id", "class", "groups", "decision", "notes"]
CLASSES = {"butterfly", "lookalike", "unrelated"}


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, fields):
    """Replace atomically so an interrupted run does not truncate the previous CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                     dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def unique_index(rows, label):
    result = {}
    for row in rows:
        key = str(row.get("photo_id", ""))
        if not key.isdigit() or key in result:
            raise ValueError(f"{label}: invalid or repeated photo_id {key!r}")
        result[key] = row
    return result


def request_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
