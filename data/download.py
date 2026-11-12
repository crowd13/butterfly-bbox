"""Download original images and verify complete decoding; no file hashing."""

import argparse
import shutil
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from common import DOWNLOAD_FIELDS, HEADERS, ROOT, read_csv, unique_index, write_csv


def inspect_image(path):
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        orientation = image.getexif().get(274, 1)
    # Coordinates will refer to the visually oriented image, as in the browser.
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return width, height, orientation


def download_one(row, image_dir):
    result = dict.fromkeys(DOWNLOAD_FIELDS, "")
    result["photo_id"] = row["photo_id"]
    if row["license_code"] != "cc0":
        return {**result, "status": "error", "error": "photo license is not cc0"}
    suffix = Path(urllib.parse.urlsplit(row["image_url"]).path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif"):
        return {**result, "status": "error", "error": f"unsupported extension: {suffix}"}
    path = image_dir / f"{row['photo_id']}{suffix}"
    result["local_path"] = str(path.resolve())
    temporary = path.with_suffix(path.suffix + ".part")
    for attempt in range(3):
        try:
            if path.exists():
                try:
                    dimensions = inspect_image(path)
                except Exception:
                    path.unlink()
                    raise
            else:
                request = urllib.request.Request(row["image_url"], headers=HEADERS)
                with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                dimensions = inspect_image(temporary)
                temporary.replace(path)
            result.update(zip(("width", "height", "exif_orientation"), dimensions))
            return {**result, "status": "ok", "error": ""}
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            result.update(status="error", error=f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "candidates.csv")
    parser.add_argument("--images", type=Path, default=ROOT / "images")
    parser.add_argument("--output", type=Path, default=ROOT / "downloads.csv")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 16:
        parser.error("workers must be between 1 and 16")
    rows = read_csv(args.input)
    unique_index(rows, "candidates")
    args.images.mkdir(parents=True, exist_ok=True)
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download_one, row, args.images) for row in rows]
        for future in as_completed(futures):
            result = future.result()
            results[result["photo_id"]] = result
            write_csv(args.output, [results[row["photo_id"]] for row in rows
                                    if row["photo_id"] in results], DOWNLOAD_FIELDS)
            if result["status"] == "error" or len(results) % 20 == 0:
                print(f"{len(results)}/{len(rows)}: {result['photo_id']} {result['status']} {result['error']}", flush=True)
    failed = sum(r["status"] != "ok" for r in results.values())
    print(f"Checked {len(results)} images; errors: {failed}. Report: {args.output}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
