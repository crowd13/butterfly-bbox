import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from crowd.review.instructions import instruction_tokens


def verify_upload(operation, expected_count):
    if operation.get("status") != "SUCCESS":
        raise ValueError(f"Upload {operation.get('id', '?')} has not completed")
    details = operation.get("details", {})
    for key in ("not_valid_count", "failed_count"):
        if details.get(key, 0):
            raise ValueError(f"Upload {operation['id']} contains {key}")
    for key in ("total_count", "success_count"):
        if key in details and details[key] != expected_count:
            raise ValueError(f"Upload {operation['id']} has unexpected {key}")


def task_content(task):
    result = {"input_values": task["input_values"]}
    if task.get("known_solutions"):
        result["known_solutions"] = [
            {
                "output_values": solution["output_values"],
                "correctness_weight": float(solution.get("correctness_weight", 1)),
            }
            for solution in task["known_solutions"]
        ]
    if task.get("message_on_unknown_solution"):
        result["message_on_unknown_solution"] = task["message_on_unknown_solution"]
    return result


def fingerprint(item, suites):
    if suites:
        result = {
            "tasks": sorted(
                json.dumps(task_content(task), sort_keys=True, ensure_ascii=False)
                for task in item["tasks"]
            ),
            "unavailable_for": sorted(item.get("unavailable_for", [])),
            "reserved_for": sorted(item.get("reserved_for", [])),
            "overlap": item.get("overlap"),
            "infinite_overlap": item.get("infinite_overlap", False),
        }
    else:
        result = {
            **task_content(item),
            "infinite_overlap": item.get("infinite_overlap", False),
        }
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


def verify_tasks(api, path, pool_id, expected):
    remote = list(api.items(path, pool_id=pool_id))
    suites = path == "/task-suites"
    if Counter(fingerprint(row, suites) for row in remote) != Counter(
        fingerprint(row, suites) for row in expected
    ):
        raise ValueError(f"Uploaded content differs in pool {pool_id}; use a new run")


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


def normalize_project(project):
    project = copy.deepcopy(project)
    project.setdefault("assignments_issuing_type", "AUTOMATED")
    project.setdefault("assignments_automerge_enabled", False)
    spec = project.get("task_spec", {})
    if not isinstance(spec, dict):
        return project
    for name in ("input_spec", "output_spec"):
        for field in spec.get(name, {}).values():
            if not isinstance(field, dict):
                continue
            field.setdefault("required", True)
            field.setdefault("hidden", False)
            if isinstance(field.get("allowed_values"), list):
                field["allowed_values"] = sorted(
                    field["allowed_values"], key=lambda value: json.dumps(value, sort_keys=True)
                )
    view = spec.get("view_spec")
    if isinstance(view, dict):
        settings = view.setdefault("settings", {})
        if isinstance(settings, dict):
            for key in ("showSkip", "showTimer", "showSubmit"):
                settings.setdefault(key, True)
    return project


def summarize(value):
    if isinstance(value, str) and len(value) > 160:
        return {
            "type": "string", "length": len(value),
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
        }
    return value


def differences(actual, expected, path=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key, value in expected.items():
            field = f"{path}.{key}" if path else key
            if key not in actual:
                result.append({"field": field, "expected": summarize(value), "missing": True})
            else:
                result.extend(differences(actual[key], value, field))
        if path in {"task_spec.input_spec", "task_spec.output_spec"}:
            result.extend(
                {"field": f"{path}.{key}", "unexpected": True}
                for key in actual.keys() - expected.keys()
            )
        return result
    if isinstance(expected, list) and isinstance(actual, list) and len(actual) == len(expected):
        return [
            diff for index, (a, b) in enumerate(zip(actual, expected, strict=True))
            for diff in differences(a, b, f"{path}[{index}]")
        ]
    if actual == expected:
        return []
    if path == "public_instructions":
        if isinstance(actual, str) and isinstance(expected, str) and (
            instruction_tokens(actual) == instruction_tokens(expected)
        ):
            return []
        return [{"field": path, "expected": expected, "actual": actual}]
    return [{"field": path, "expected": summarize(expected), "actual": summarize(actual)}]


def verify_resources(api, state, report_path=None):
    statuses = {}
    reports = []
    for key, resource_id in state["resources"].items():
        path = {
            "project": "/projects",
            "training": "/trainings",
            "exam_skill": "/skills",
            "access_skill": "/skills",
        }.get(key, "/pools")
        resource = api.request("GET", f"{path}/{resource_id}")
        expected = dict(state["definitions"][key])
        if path == "/pools":
            expected.pop("will_expire", None)
        if key == "project":
            resource, expected = normalize_project(resource), normalize_project(expected)
        mismatches = differences(resource, expected)
        if mismatches:
            reports.append({"resource": key, "id": resource_id, "differences": mismatches})
        if resource.get("status"):
            statuses[key] = resource["status"]
    if report_path:
        Path(report_path).write_text(
            json.dumps({"verified": not reports, "statuses": statuses, "mismatches": reports},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if reports:
        fields = ", ".join(
            row["resource"] + "." + diff["field"]
            for row in reports for diff in row["differences"]
        )
        suffix = f"; report: {report_path}" if report_path else ""
        raise ValueError(f"Remote settings differ: {fields}{suffix}")
    return statuses
