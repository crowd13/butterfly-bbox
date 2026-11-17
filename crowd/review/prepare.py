"""Build deterministic review pages from exported bbox answers."""

import json
import random
from decimal import Decimal
from pathlib import Path

VERDICTS = {"OK", "MISSING", "EXTRA", "BOUNDARY", "LOAD_ERROR"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_answer(row):
    required = {
        "answer_id",
        "photo_id",
        "image_url",
        "width",
        "height",
        "worker_id",
        "solution",
    }
    if required - row.keys():
        raise ValueError(f"Incomplete bbox answer: {row.get('photo_id', '?')}")
    solution = row["solution"]
    if solution.get("status") not in {"boxes", "no_butterflies", "uncertain"}:
        raise ValueError(f"Unsupported bbox status: {row['photo_id']}")
    boxes = solution.get("boxes")
    if not isinstance(boxes, list):
        raise ValueError(f"Invalid boxes: {row['photo_id']}")
    for box in boxes:
        values = [box.get(key) for key in ("left", "top", "width", "height")]
        if not all(isinstance(value, (int, float)) for value in values):
            raise ValueError(f"Invalid box: {row['photo_id']}")
        left, top, width, height = values
        if (
            min(values) < 0
            or width <= 0
            or height <= 0
            or left + width > 1
            or top + height > 1
        ):
            raise ValueError(f"Box outside image: {row['photo_id']}")


def public_input(row):
    return {
        "answer_id": row["answer_id"],
        "photo_id": str(row["photo_id"]),
        "image_url": row["image_url"],
        "width": int(row["width"]),
        "height": int(row["height"]),
        "source_status": row["solution"]["status"],
        "source_boxes": row["solution"]["boxes"],
    }


def altered(solution, verdict):
    boxes = [dict(box) for box in solution["boxes"]]
    if verdict == "OK":
        return solution["status"], boxes
    if verdict == "MISSING":
        if not boxes:
            raise ValueError("MISSING fixture needs a box")
        boxes = boxes[:-1]
        return ("boxes" if boxes else "no_butterflies"), boxes
    if verdict == "EXTRA":
        if boxes:
            raise ValueError("EXTRA fixture needs an empty image")
        return "boxes", [{"left": 0.3, "top": 0.3, "width": 0.4, "height": 0.4}]
    if verdict == "BOUNDARY":
        if not boxes:
            raise ValueError("BOUNDARY fixture needs a box")
        box = boxes[0]
        box["left"] += box["width"] * 0.25
        box["top"] += box["height"] * 0.25
        box["width"] *= 0.5
        box["height"] *= 0.5
        return "boxes", boxes
    raise ValueError(f"Cannot generate fixture: {verdict}")


def fixtures(bank, specs):
    rows = {str(row["photo_id"]): row for split in bank.values() for row in split}
    result = []
    for spec in specs:
        verdict = spec["verdict"]
        if verdict not in VERDICTS - {"LOAD_ERROR"}:
            raise ValueError(f"Invalid fixture verdict: {verdict}")
        source = rows.get(str(spec["photo_id"]))
        if source is None:
            raise ValueError(f"Unknown gold photo: {spec['photo_id']}")
        status, boxes = altered(source["solution"], verdict)
        row = dict(source)
        row.update(
            answer_id=f"gold:{source['photo_id']}:{verdict}",
            worker_id="gold",
            solution={"status": status, "boxes": boxes},
        )
        result.append({**public_input(row), "verdict": verdict, "hint": spec["hint"]})
    if len({row["photo_id"] for row in result}) != len(result):
        raise ValueError("Fixture photos must be unique")
    return result


def control_specs(bank, excluded, count):
    hints = {
        "OK": "Исходная разметка корректна.",
        "MISSING": "Взрослая бабочка пропущена.",
        "EXTRA": "Рамка поставлена на нецелевой объект.",
        "BOUNDARY": "Рамка заметно обрезает бабочку.",
    }
    positive = ["OK", "MISSING", "BOUNDARY"]
    negative = ["OK", "EXTRA"]
    positions = {"positive": 0, "negative": 0}
    result = []
    for row in (item for split in bank.values() for item in split):
        if str(row["photo_id"]) in excluded:
            continue
        kind = "positive" if row["solution"]["boxes"] else "negative"
        choices = positive if kind == "positive" else negative
        verdict = choices[positions[kind] % len(choices)]
        positions[kind] += 1
        result.append(
            {
                "photo_id": str(row["photo_id"]),
                "verdict": verdict,
                "hint": hints[verdict],
            }
        )
        if len(result) == count:
            return result
    raise ValueError("Not enough unique gold photos for controls")


def pages(rows, controls, config, seed=None):
    for row in rows:
        validate_answer(row)
    if len(rows) != config["dataset_size"] or len({r["photo_id"] for r in rows}) != len(
        rows
    ):
        raise ValueError("Expected unique answers for the complete dataset")
    shuffled = list(rows)
    random.Random(config["seed"] if seed is None else seed).shuffle(shuffled)
    size = config["work_per_page"]
    if len(shuffled) % size:
        raise ValueError("Dataset must fill complete review pages")

    result = []
    for index in range(0, len(shuffled), size):
        work = shuffled[index : index + size]
        control = controls[(index // size) % len(controls)]
        tasks = [{"input_values": public_input(row)} for row in work]
        tasks.append(
            {
                "input_values": {
                    key: value
                    for key, value in control.items()
                    if key not in {"verdict", "hint"}
                },
                "known_solutions": [
                    {
                        "output_values": {"verdict": control["verdict"]},
                        "correctness_weight": 1.0,
                    }
                ],
            }
        )
        result.append(
            {
                "tasks": tasks,
                "overlap": config["overlap"],
                "infinite_overlap": False,
                "unavailable_for": sorted({row["worker_id"] for row in work}),
            }
        )
    return result


def budget(config, page_count):
    paid = Decimal(str(config["review_price"])) * page_count * config["overlap"]
    exam = Decimal(str(config["exam_price"])) * config["exam_attempts"]
    total = (paid + exam) * (1 + Decimal(str(config["commission"])))
    return {"work": str(paid), "exam": str(exam), "total_with_commission": str(total)}
