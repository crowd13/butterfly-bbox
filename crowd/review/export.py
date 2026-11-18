"""Export review verdicts and enforce reviewer independence."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from crowd.review.prepare import VERDICTS, public_input, read_json, validate_answer


def export(assignments, source):
    for row in source:
        validate_answer(row)
    authors = {row["answer_id"]: row["worker_id"] for row in source}
    if len(authors) != len(source) or len({str(row["photo_id"]) for row in source}) != len(source):
        raise ValueError("Source answer and photo IDs must be unique")
    if any(not isinstance(worker, str) or not worker.strip() for worker in authors.values()):
        raise ValueError("Source worker IDs must be nonempty strings")
    expected = {row["answer_id"]: public_input(row) for row in source}
    reviews = defaultdict(list)
    seen = set()
    for assignment in assignments:
        if assignment["status"] != "ACCEPTED":
            continue
        if assignment["id"] in seen:
            raise ValueError(f"Duplicate assignment: {assignment['id']}")
        seen.add(assignment["id"])
        worker = assignment["user_id"]
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError(f"Missing reviewer: {assignment['id']}")
        for task, solution in zip(
            assignment["tasks"], assignment["solutions"], strict=True
        ):
            answer_id = task["input_values"]["answer_id"]
            if answer_id not in authors:
                if isinstance(answer_id, str) and answer_id.startswith("gold:"):
                    continue
                raise ValueError(f"Unknown reviewed answer: {answer_id}")
            if task["input_values"] != expected[answer_id]:
                raise ValueError(f"Reviewed input does not match source: {answer_id}")
            if worker == authors[answer_id]:
                raise ValueError(f"Self-review detected: {answer_id}")
            reviews[answer_id].append(
                {
                    "assignment_id": assignment["id"],
                    "worker_id": worker,
                    "verdict": solution["output_values"].get("verdict"),
                }
            )

    result = []
    for row in source:
        values = reviews[row["answer_id"]]
        workers = {value["worker_id"] for value in values}
        if len(values) != 2 or len(workers) != 2:
            raise ValueError(f"Expected two independent reviews: {row['answer_id']}")
        verdicts = [value["verdict"] for value in values]
        if any(not isinstance(value, str) or value not in VERDICTS for value in verdicts):
            raise ValueError(f"Invalid verdict: {row['answer_id']}")
        result.append(
            {
                "answer_id": row["answer_id"],
                "photo_id": row["photo_id"],
                "source_solution": row["solution"],
                "source_worker_id": row["worker_id"],
                "reviews": values,
                "decision": (
                    "ACCEPT"
                    if verdicts == ["OK", "OK"] and row["solution"]["status"] != "uncertain"
                    else "MANUAL"
                ),
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assignments", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = export(read_json(args.assignments), read_json(args.source))
    args.output.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Exported {len(rows)} reviews: {args.output}")


if __name__ == "__main__":
    main()
