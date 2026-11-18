import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from crowd.review.export import export
from crowd.review.interface_update import write_json
from crowd.review.prepare import VERDICTS, public_input, read_json, validate_answer


def audit(assignments, source, plan, resources):
    report = {
        "ready": False, "source_photos": len(source),
        "assignment_statuses": dict(Counter(row.get("status", "UNKNOWN") for row in assignments)),
        "errors": [], "pools": {}, "control_quality": {},
    }
    authors = {}
    expected_inputs = {}
    try:
        for row in source:
            validate_answer(row)
            if row["answer_id"] in authors:
                raise ValueError("Duplicate source answer ID")
            authors[row["answer_id"]] = row
            expected_inputs[row["answer_id"]] = public_input(row)
        if len({str(row["photo_id"]) for row in source}) != len(source):
            raise ValueError("Duplicate source photo ID")
        if len(source) != plan["config"]["dataset_size"]:
            raise ValueError("Source does not cover the configured dataset")
    except (KeyError, TypeError, ValueError) as error:
        report["errors"].append(str(error))
        return report

    seen = set()
    expected_all = []
    quality = defaultdict(lambda: {"correct": 0, "total": 0, "pools": {}})
    for stage in ("pilot", "main"):
        pool_id = resources[stage]
        rows = [row for row in assignments if row.get("pool_id") == pool_id]
        suites = {}
        expected_ids = []
        for suite in plan[stage]:
            tasks = {task["input_values"]["answer_id"]: task for task in suite["tasks"]}
            if len(tasks) != len(suite["tasks"]):
                report["errors"].append(f"Duplicate task in frozen {stage} plan")
            suites[tuple(sorted(tasks))] = tasks
            expected_ids.extend(key for key in tasks if key in authors)
        expected_all.extend(expected_ids)
        pool = {
            "pool_id": pool_id, "ready": False, "expected_photos": len(expected_ids),
            "assignment_statuses": dict(Counter(row.get("status", "UNKNOWN") for row in rows)),
            "accepted_assignments": 0, "valid_assignments": 0, "errors": [],
            "photos_with_reviews": 0, "two_independent_reviews": 0,
            "missing_photo_ids": [], "incomplete": [], "disagreements": 0,
            "verdicts": {}, "decisions": {}, "manual_cases": [], "photos": [],
        }
        report["pools"][stage] = pool
        votes = defaultdict(list)
        for assignment in rows:
            if assignment.get("status") != "ACCEPTED":
                continue
            pool["accepted_assignments"] += 1
            assignment_id = assignment.get("id")
            worker = assignment.get("user_id")
            try:
                if not isinstance(assignment_id, str) or not assignment_id:
                    raise ValueError("Missing assignment ID")
                if assignment_id in seen:
                    raise ValueError("Duplicate accepted assignment")
                seen.add(assignment_id)
                if not isinstance(worker, str) or not worker.strip():
                    raise ValueError("Missing reviewer")
                actual_tasks = assignment["tasks"]
                solutions = assignment["solutions"]
                ids = [task["input_values"]["answer_id"] for task in actual_tasks]
                expected = suites.get(tuple(sorted(ids)))
                if expected is None or len(ids) != len(set(ids)):
                    raise ValueError("Assignment tasks differ from the frozen page")
                if len(actual_tasks) != len(solutions):
                    raise ValueError("Task and solution counts differ")
                checked = []
                for task, solution in zip(actual_tasks, solutions, strict=True):
                    key = task["input_values"]["answer_id"]
                    if task["input_values"] != expected[key]["input_values"]:
                        raise ValueError(f"Task input changed: {key}")
                    verdict = solution["output_values"].get("verdict")
                    if not isinstance(verdict, str) or verdict not in VERDICTS:
                        raise ValueError(f"Invalid verdict: {key}")
                    if key in authors:
                        if task["input_values"] != expected_inputs[key]:
                            raise ValueError(f"Frozen source differs from page: {key}")
                        if worker == authors[key]["worker_id"]:
                            raise ValueError(f"Self-review: {key}")
                    checked.append((key, verdict))
                for key, verdict in checked:
                    if key in authors:
                        votes[key].append({
                            "assignment_id": assignment_id, "worker_id": worker, "verdict": verdict,
                        })
                    else:
                        golden = expected[key].get("known_solutions", [])
                        correct = any(
                            item["output_values"].get("verdict") == verdict for item in golden
                        )
                        stats = quality[worker]
                        by_pool = stats["pools"].setdefault(stage, {"correct": 0, "total": 0})
                        for value in (stats, by_pool):
                            value["total"] += 1
                            value["correct"] += correct
                pool["valid_assignments"] += 1
            except (KeyError, TypeError, ValueError) as error:
                pool["errors"].append({"assignment_id": assignment_id, "message": str(error)})

        verdict_counts, decisions = Counter(), Counter()
        for key in expected_ids:
            row, values = authors[key], votes[key]
            independent = len(values) == 2 and len({value["worker_id"] for value in values}) == 2
            entry = {"answer_id": key, "photo_id": row["photo_id"], "reviews": values}
            pool["photos_with_reviews"] += bool(values)
            pool["two_independent_reviews"] += independent
            verdict_counts.update(value["verdict"] for value in values)
            if not values:
                pool["missing_photo_ids"].append(row["photo_id"])
            if not independent:
                pool["incomplete"].append(entry)
                entry["decision"] = None
            else:
                verdicts = [value["verdict"] for value in values]
                disagree = len(set(verdicts)) != 1
                decision = "ACCEPT" if verdicts == ["OK", "OK"] and (
                    row["solution"]["status"] != "uncertain"
                ) else "MANUAL"
                entry.update(decision=decision, disagreement=disagree)
                decisions[decision] += 1
                pool["disagreements"] += disagree
                if decision == "MANUAL":
                    pool["manual_cases"].append(entry)
            pool["photos"].append(entry)
        pool["verdicts"], pool["decisions"] = dict(verdict_counts), dict(decisions)
        pool["ready"] = not pool["errors"] and not pool["incomplete"] and not any(
            pool["assignment_statuses"].get(status, 0) for status in ("ACTIVE", "SUBMITTED")
        )

    if len(expected_all) != len(set(expected_all)) or set(expected_all) != set(authors):
        report["errors"].append("Frozen pilot/main pages do not partition the source dataset")
    known_pools = {resources[stage] for stage in ("pilot", "main")}
    unknown = [row.get("id") for row in assignments if row.get("pool_id") not in known_pools]
    if unknown:
        report["errors"].append({"unknown_pool_assignments": unknown})
    for stats in quality.values():
        stats["accuracy"] = 100 * stats["correct"] / stats["total"]
        for value in stats["pools"].values():
            value["accuracy"] = 100 * value["correct"] / value["total"]
        stats["minimum_answers"] = plan["config"]["control_min_answers"]
        stats["threshold"] = plan["config"]["control_pass"]
        stats["accuracy_kind"] = "cumulative_observation"
        stats["sufficient_answers"] = stats["total"] >= stats["minimum_answers"]
        stats["assessment"] = (
            "insufficient_data" if not stats["sufficient_answers"] else
            "at_or_above_threshold" if stats["accuracy"] >= stats["threshold"] else
            "below_threshold"
        )
    report["control_quality"] = dict(quality)
    report["ready"] = not report["errors"] and all(
        pool["ready"] for pool in report["pools"].values()
    )
    report["decision_counts"] = dict(sum(
        (Counter(pool["decisions"]) for pool in report["pools"].values()), Counter()
    ))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    report = {"ready": False, "errors": [], "pools": {}}
    outputs = {
        "reviews": args.run / "reviews.json", "pilot_reviews": args.run / "pilot-reviews.json"
    }
    written = set()
    try:
        assignments = read_json(args.run / "assignments.json")
        source = read_json(args.run / "source.json")
        plan = read_json(args.run / "snapshot.json")["plan"]
        resources = read_json(args.run / "state.json")["resources"]
        report = audit(assignments, source, plan, resources)
        report["input_sha256"] = hashlib.sha256(json.dumps(
            [assignments, source, plan, resources], sort_keys=True, ensure_ascii=False
        ).encode()).hexdigest()
        if not report["errors"] and report["pools"]["pilot"]["ready"]:
            ids = {row["answer_id"] for row in report["pools"]["pilot"]["photos"]}
            pilot_source = [row for row in source if row["answer_id"] in ids]
            pilot_assignments = [
                row for row in assignments if row.get("pool_id") == resources["pilot"]
            ]
            write_json(outputs["pilot_reviews"], export(pilot_assignments, pilot_source))
            written.add("pilot_reviews")
        if report["ready"] and len(source) == 400:
            write_json(outputs["reviews"], export(assignments, source))
            written.add("reviews")
    except (OSError, KeyError, TypeError, ValueError) as error:
        report["ready"] = False
        report.setdefault("errors", []).append(str(error))
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["artifacts"] = {
        key: {"path": str(path), "exists": path.exists(), "current": key in written}
        for key, path in outputs.items()
    }
    write_json(args.run / "audit.json", report)
    print(json.dumps({
        "ready": report["ready"], "audit": str(args.run / "audit.json"),
        "pools": {key: {field: value[field] for field in (
            "ready", "expected_photos", "two_independent_reviews", "decisions", "disagreements"
        )} for key, value in report["pools"].items()},
        "errors": report["errors"], "artifacts": report["artifacts"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
