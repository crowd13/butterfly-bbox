"""Manifest selection, human gold records and image preflight."""

import csv
import hashlib
import io
import random
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageOps

from .budget import plan
from .quality import evaluate, validate_solution

DIFFICULT_SCENARIOS = {"small", "multiple", "occluded", "cropped"}
SCENARIOS = DIFFICULT_SCENARIOS | {"ordinary", "moth", "lookalike", "empty"}


def image_groups(row):
    return {g.strip() for g in row.get("groups", "").split(";") if g.strip()}


def is_difficult(row):
    return row["class"] == "butterfly" and bool(image_groups(row) & DIFFICULT_SCENARIOS)


def has_moth(row):
    return "moth" in (
        image_groups(row)
        | set(row.get("source_groups", "").split(";"))
        | set(row.get("scenarios", []))
    )


def read_manifest(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def validate_image(row):
    if has_moth(row) and row.get("class") != "butterfly":
        raise ValueError("Adult moths must be targets; update the manifest")
    if not str(row.get("photo_id", "")).isdigit():
        raise ValueError("Missing numeric photo_id")
    if row.get("license_code") != "cc0":
        raise ValueError(f"{row['photo_id']}: expected CC0")
    url = urlparse(row.get("image_url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Images need public HTTPS URLs without credentials")
    for key in ("width", "height"):
        if int(row.get(key, 0)) <= 0:
            raise ValueError(f"Invalid {key}")


def select_manifest(rows, seed=42):
    """Keep every marked difficult butterfly, sample the remaining strata."""
    rng = random.Random(seed)
    strata = {"hard": [], "ordinary": [], "lookalike": [], "unrelated": []}
    for row in rows:
        if has_moth(row) and row["class"] != "butterfly":
            raise ValueError("Adult moths must be targets; update the manifest")
        key = row["class"]
        if key == "butterfly":
            key = "hard" if is_difficult(row) else "ordinary"
        strata[key].append(row)
    if len(strata["hard"]) > 303:
        raise ValueError("Too many difficult images for the agreed selection")
    targets = {
        "hard": len(strata["hard"]),
        "ordinary": 303 - len(strata["hard"]),
        "lookalike": 57,
        "unrelated": 40,
    }
    chosen = []
    for key, count in targets.items():
        if len(strata[key]) < count:
            raise ValueError(f"Not enough images in {key}")
        chosen.extend(rng.sample(sorted(strata[key], key=lambda r: int(r["photo_id"])), count))
    # Start with rare scenarios; then interleave difficulty to avoid all-hard pages.
    hard = [r for r in chosen if is_difficult(r)]
    easy = [r for r in chosen if r not in hard]
    counts = Counter(g for r in hard for g in image_groups(r) & DIFFICULT_SCENARIOS)
    rng.shuffle(hard)
    hard.sort(key=lambda r: min(counts[g] for g in image_groups(r) & DIFFICULT_SCENARIOS))
    # Put a representative of each difficult scenario in the pilot, then retain
    # rarity order. Sampling alone can omit an entire scenario from the first 40.
    representatives = []
    covered = set()
    for row in hard:
        scenarios = image_groups(row) & DIFFICULT_SCENARIOS
        if scenarios - covered:
            representatives.append(row)
            covered.update(scenarios)
    hard = representatives + [r for r in hard if r not in representatives]
    rng.shuffle(easy)
    representatives = [
        next(r for r in easy if r["class"] == key)
        for key in ("butterfly", "lookalike", "unrelated")
    ]
    moth = next((r for r in easy if "moth" in image_groups(r)), None)
    if moth is not None and moth not in representatives:
        representatives.append(moth)
    easy = representatives + [r for r in easy if r not in representatives]
    result = []
    while hard or easy:
        if hard:
            result.append(hard.pop(0))
        for _ in range(3):
            if easy:
                result.append(easy.pop(0))
    return result


def validate_inputs(manifest, bank, config):
    for field in (
        "dataset_size",
        "seed",
        "real_per_page",
        "pilot_size",
        "exam_attempts",
        "exam_size",
        "practice_size",
        "exam_pass",
        "control_window",
        "control_min_correct",
        "min_workers",
        "repeat_limit",
        "pilot_min_correct",
        "assignment_seconds",
    ):
        if type(config[field]) is not int or config[field] < (
            0 if field in {"seed", "repeat_limit"} else 1
        ):
            raise ValueError(f"Invalid integer: {field}")
    plan(config)
    if not 0 < config["iou_threshold"] <= 1 or config["exam_pass"] > 100:
        raise ValueError("Invalid quality threshold")
    if not 0 < config["pilot_size"] < config["dataset_size"]:
        raise ValueError("Pilot must leave images for the main pool")
    if config["pilot_min_correct"] > config["pilot_size"]:
        raise ValueError("Pilot threshold exceeds its size")
    if config["control_min_correct"] > config["control_window"]:
        raise ValueError("Invalid control window")
    if len(manifest) != config["dataset_size"]:
        raise ValueError(f"Freeze exactly {config['dataset_size']} images first (use select)")
    if (
        len(bank.get("practice", [])) != config["practice_size"]
        or len(bank.get("exam", [])) != config["exam_size"]
    ):
        raise ValueError("Expected 10 practice and 10 exam records")
    budget = plan(config)
    required_controls = max(
        budget["pilot_pages"], budget["remaining_pages"], budget["repeat_pages"]
    )
    if len(bank.get("controls", [])) < required_controls:
        raise ValueError(
            f"Need at least {required_controls} distinct controls for the largest pool"
        )
    all_rows = manifest + bank["practice"] + bank["exam"] + bank["controls"]
    ids, urls = set(), set()
    for row in all_rows:
        validate_image(row)
        pid = str(row["photo_id"])
        if pid in ids or row["image_url"] in urls:
            raise ValueError("Repeated photo ID or URL across dataset/gold splits")
        ids.add(pid)
        urls.add(row["image_url"])
    for split in ("practice", "exam", "controls"):
        scenarios = set()
        for row in bank[split]:
            validate_solution(row["solution"])
            if has_moth(row) and row["solution"]["status"] != "boxes":
                raise ValueError("Adult moth gold requires bounding boxes")
            if row["solution"]["status"] not in {"boxes", "no_butterflies"}:
                raise ValueError("Gold must be unambiguous")
            reviews = row.get("independent_reviews", [])
            if len(reviews) != 2 or len({r.get("reviewer") for r in reviews}) != 2:
                raise ValueError("Two independent human annotations are required")
            for review in reviews:
                if not review.get("reviewer", "").strip():
                    raise ValueError("Missing reviewer")
                validate_solution(review["solution"])
                if (
                    not evaluate(review["solution"], row["solution"], config["iou_threshold"])[
                        "correct"
                    ]
                    and not row.get("resolution", "").strip()
                ):
                    raise ValueError("Explain how disagreement was resolved")
            if not row.get("hint", "").strip():
                raise ValueError("Gold requires an explanation")
            scenarios.update(row.get("scenarios", []))
        if not SCENARIOS <= scenarios:
            raise ValueError(f"{split}: missing scenarios {sorted(SCENARIOS - scenarios)}")


def image_preflight(rows):
    """Decode actual public images; dimensions follow browser EXIF orientation."""
    records, hashes = [], {}
    for row in rows:
        request = urllib.request.Request(
            row["image_url"], headers={"User-Agent": "butterfly-bbox/1.0"}
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read(40 * 1024 * 1024 + 1)
        if len(raw) > 40 * 1024 * 1024:
            raise ValueError("Image exceeds preflight size limit")
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            picture = ImageOps.exif_transpose(source).convert("RGB")
            if picture.size != (int(row["width"]), int(row["height"])):
                raise ValueError(f"{row['photo_id']}: oriented dimensions do not match manifest")
            fingerprint = hashlib.sha256(str(picture.size).encode() + picture.tobytes()).hexdigest()
        if fingerprint in hashes:
            raise ValueError(f"Duplicate pixels: {row['photo_id']} and {hashes[fingerprint]}")
        hashes[fingerprint] = str(row["photo_id"])
        records.append(
            {
                "photo_id": str(row["photo_id"]),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "pixels_sha256": fingerprint,
            }
        )
    return records
