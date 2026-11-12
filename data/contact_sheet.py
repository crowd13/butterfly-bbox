"""Build an offline HTML review page with thumbnails and CSV export."""

import argparse
import json
import os
import urllib.parse
from pathlib import Path

from PIL import Image, ImageOps

from common import ROOT, read_csv, unique_index


def relative_url(path, parent):
    return urllib.parse.quote(Path(os.path.relpath(path, parent)).as_posix())


def build_rows(candidates, downloads, output):
    reports = unique_index(downloads, "downloads")
    unique_index(candidates, "candidates")
    thumbnail_dir = output.parent / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for candidate in candidates:
        report = reports.get(candidate["photo_id"], {})
        row = {**candidate, "status": report.get("status", "not downloaded"),
               "error": report.get("error", ""), "thumbnail": "", "original": ""}
        if report.get("status") == "ok":
            path = Path(report["local_path"])
            thumb = thumbnail_dir / f"{candidate['photo_id']}.jpg"
            try:
                if not thumb.exists():
                    with Image.open(path) as original:
                        image = ImageOps.exif_transpose(original).convert("RGB")
                        image.thumbnail((480, 360))
                        image.save(thumb, quality=80)
                row["thumbnail"] = relative_url(thumb, output.parent)
                row["original"] = relative_url(path, output.parent)
            except Exception as exc:
                row.update(status="error", error=f"thumbnail: {exc}")
        rows.append(row)
    return rows


TEMPLATE = (ROOT / "review_template.html").read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "candidates.csv")
    parser.add_argument("--downloads", type=Path, default=ROOT / "downloads.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "review.html")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows(read_csv(args.input), read_csv(args.downloads), args.output)
    # Prevent metadata from ending the inline script.
    payload = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")
    args.output.write_text(TEMPLATE.replace("__PHOTOS__", payload), encoding="utf-8")
    print(f"Open in browser: {args.output.resolve()}")


if __name__ == "__main__":
    main()
