"""Geometry and deterministic quality rules; no API or filesystem access."""

import math

STATUSES = {"boxes", "no_butterflies", "uncertain", "load_error"}


def validate_solution(solution):
    if not isinstance(solution, dict) or solution.get("status") not in STATUSES:
        raise ValueError("Choose a valid answer status")
    boxes = solution.get("boxes")
    if not isinstance(boxes, list) or len(boxes) > 200:
        raise ValueError("boxes must be a list of at most 200 rectangles")
    for box in boxes:
        if not isinstance(box, dict) or set(box) != {"left", "top", "width", "height"}:
            raise ValueError("A box needs left, top, width, height")
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in box.values()):
            raise ValueError("Coordinates must be finite numbers")
        x, y, w, h = (box[k] for k in ("left", "top", "width", "height"))
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.00000001 or y + h > 1.00000001:
            raise ValueError("Rectangle outside image or empty")
    if solution["status"] == "boxes" and not boxes:
        raise ValueError("Draw at least one butterfly")
    if solution["status"] in {"no_butterflies", "load_error"} and boxes:
        raise ValueError("This status requires an empty list")
    return solution


def iou(a, b):
    width = max(0, min(a["left"] + a["width"], b["left"] + b["width"]) - max(a["left"], b["left"]))
    height = max(0, min(a["top"] + a["height"], b["top"] + b["height"]) - max(a["top"], b["top"]))
    intersection = width * height
    return intersection / (a["width"] * a["height"] + b["width"] * b["height"] - intersection)


def match_count(predicted, expected, threshold):
    """Maximum cardinality bipartite matching (not greedy IoU selection)."""
    edges = [
        [j for j, gold in enumerate(expected) if iou(box, gold) >= threshold] for box in predicted
    ]
    owners = {}

    def augment(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owners or augment(owners[j], seen):
                owners[j] = i
                return True
        return False

    return sum(augment(i, set()) for i in range(len(predicted)))


def evaluate(solution, gold, threshold=0.85):
    try:
        validate_solution(solution)
    except ValueError as exc:
        return {"correct": False, "reason": str(exc)}
    if solution["status"] == "load_error":
        return {"correct": None, "reason": "load_error: human investigation required"}
    if solution["status"] == "uncertain":
        return {"correct": False, "reason": "uncertain on an unambiguous control"}
    tp = match_count(solution["boxes"], gold["boxes"], threshold)
    correct = solution["status"] == gold["status"] and tp == len(solution["boxes"]) == len(
        gold["boxes"]
    )
    return {
        "correct": correct,
        "tp": tp,
        "fp": len(solution["boxes"]) - tp,
        "fn": len(gold["boxes"]) - tp,
        "reason": "matched" if correct else "geometry/status",
    }


def control_windows(events, window=5, minimum=4):
    """Replay all events; failure stays latched. Unknown/technical cases require audit."""
    history, failed, needs_audit = [], False, False
    for event in sorted(events, key=lambda e: (e["submitted"], e["id"])):
        result = event["correct"]
        if result is None:
            needs_audit = True
            continue
        history.append(result)
        if history[-window:].count(False) > window - minimum:
            failed = True
    return {
        "failed": failed,
        "needs_audit": needs_audit or len(history) < window,
        "tail": history[-(window - 1) :],
        "count": len(history),
    }
