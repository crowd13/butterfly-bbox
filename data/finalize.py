"""Freeze a human-reviewed manifest before bbox annotation. No automatic truncation."""

import argparse
from collections import Counter
from pathlib import Path

from common import CANDIDATE_FIELDS, CLASSES, ROOT, normalize_target, read_csv, unique_index, write_csv

FINAL_FIELDS = CANDIDATE_FIELDS + ["width", "height", "exif_orientation", "class", "groups", "notes"]


def finalize(candidates, downloads, decisions):
    candidates = unique_index(candidates, "candidates")
    downloads = unique_index(downloads, "downloads")
    decisions = unique_index(decisions, "decisions")
    if set(decisions) != set(candidates):
        raise ValueError("Decisions must contain every candidate ID exactly once, with no extra IDs")
    unresolved = [key for key, d in decisions.items() if d.get("decision") not in ("keep", "exclude")]
    if unresolved:
        raise ValueError(f"Review unfinished: {len(unresolved)} candidates still pending")
    result = []
    for key, candidate in candidates.items():
        decision, download = normalize_target(decisions[key]), downloads.get(key, {})
        if decision["decision"] == "exclude":
            continue
        if candidate["license_code"] != "cc0" or download.get("status") != "ok":
            raise ValueError(f"{key}: cannot include a non-CC0 or unsuccessfully downloaded image")
        if decision.get("class") not in CLASSES:
            raise ValueError(f"{key}: invalid class")
        groups = ";".join(dict.fromkeys(g.strip() for g in decision.get("groups", "").split(";") if g.strip()))
        result.append({**candidate, **{k: download[k] for k in ("width", "height", "exif_orientation")},
                       "class": decision["class"], "groups": groups, "notes": decision.get("notes", "")})
    if not result:
        raise ValueError("No photographs selected")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "candidates.csv")
    parser.add_argument("--downloads", type=Path, default=ROOT / "downloads.csv")
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "manifest.csv")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("manifest exists; use a new --output to create another version")
    try:
        rows = finalize(read_csv(args.input), read_csv(args.downloads), read_csv(args.decisions))
    except ValueError as exc:
        parser.error(str(exc))
    write_csv(args.output, rows, FINAL_FIELDS)
    print(f"Frozen {len(rows)} images: {dict(Counter(r['class'] for r in rows))}")
    print(f"Manifest: {args.output}")


if __name__ == "__main__":
    main()
