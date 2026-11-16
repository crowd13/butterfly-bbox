"""Lossless answer extraction and review handoff (never an aggregated golden set)."""

from .quality import validate_solution

TERMINAL_ANSWERS = {"ACCEPTED", "SUBMITTED", "REJECTED"}


def answers(assignments, snapshot):
    working = {str(r["photo_id"]) for r in snapshot["manifest"]}
    gold = {str(r["photo_id"]): split for split, rows in snapshot["bank"].items() for r in rows}
    result = []
    for assignment in assignments:
        tasks = assignment.get("tasks") or []
        solutions = assignment.get("solutions") or []
        for i, task in enumerate(tasks):
            values = task["input_values"]
            pid = str(values["photo_id"])
            role = "work" if pid in working else gold.get(pid)
            if role is None:
                raise ValueError(f"Unknown image in API response: {pid}")
            entry = solutions[i] if i < len(solutions) else None
            solution = entry.get("output_values") if isinstance(entry, dict) else None
            error = None
            try:
                validate_solution(solution)
            except ValueError as exc:
                error = str(exc)
            result.append(
                {
                    "answer_id": f"{assignment['id']}:{i}",
                    "photo_id": pid,
                    "image_url": values["image_url"],
                    "width": values["width"],
                    "height": values["height"],
                    "role": role,
                    "solution": solution,
                    "validation_error": error,
                    "worker_id": assignment["user_id"],
                    "assignment_id": assignment["id"],
                    "task_id": task.get("id"),
                    "task_suite_id": assignment["task_suite_id"],
                    "pool_id": assignment["pool_id"],
                    "assignment_status": assignment["status"],
                    "submitted": assignment.get("submitted", ""),
                    "created": assignment.get("created", ""),
                    "manifest_version": snapshot["manifest_hash"],
                    "instruction_version": snapshot["instruction_hash"],
                }
            )
    return result


def coverage(records, manifest):
    done, issues = set(), []
    for row in records:
        if row["role"] != "work" or row["assignment_status"] not in TERMINAL_ANSWERS:
            continue
        done.add(row["photo_id"])
        if row["validation_error"] or row["solution"]["status"] in {"uncertain", "load_error"}:
            issues.append(row["answer_id"])
    ids = {str(r["photo_id"]) for r in manifest}
    return {
        "expected": len(ids),
        "answered": len(done),
        "missing": sorted(ids - done),
        "issues": issues,
        "is_golden_set": False,
    }
