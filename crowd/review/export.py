"""Export review verdicts and enforce reviewer independence."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from crowd.review.prepare import read_json


def export(assignments, source):
    authors = {row["answer_id"]: row["worker_id"] for row in source}
    reviews = defaultdict(list)
    for assignment in assignments:
        if assignment["status"] != "ACCEPTED":
            continue
        worker = assignment["user_id"]
        for task, solution in zip(
            assignment["tasks"], assignment["solutions"], strict=True
        ):
            answer_id = task["input_values"]["answer_id"]
            if answer_id not in authors:
                continue
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
        if any(
            value not in {"OK", "MISSING", "EXTRA", "BOUNDARY", "LOAD_ERROR"}
            for value in verdicts
        ):
            raise ValueError(f"Invalid verdict: {row['answer_id']}")
        result.append(
            {
                "answer_id": row["answer_id"],
                "photo_id": row["photo_id"],
                "source_solution": row["solution"],
                "source_worker_id": row["worker_id"],
                "reviews": values,
                "decision": "ACCEPT" if verdicts == ["OK", "OK"] else "MANUAL",
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
