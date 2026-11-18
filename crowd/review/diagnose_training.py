import argparse
import getpass
import hashlib
import json
import re
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from crowd.bbox.api import ApiError, TasksApi
from crowd.review.interface_update import write_json


def scrub(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if re.search(r"token|password|authorization|cookie|secret|csrf", key, re.I)
            else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(?:y[01]_|t[01]_|AQAD-)[A-Za-z0-9_.-]{12,}", "<redacted>", value)
    return value


def owner_fields(row):
    owner = row.get("owner") or {}
    return {key: owner[key] for key in ("id", "myself") if key in owner}


def owner_matches(owner, requester_id):
    if owner.get("id") is not None and requester_id is not None:
        return str(owner["id"]) == str(requester_id)
    return owner.get("myself")


def collect(api, project_id, reference_id=None, include_other_projects=True):
    reference_id = project_id if reference_id is None else reference_id
    errors = []

    def read(path, **filters):
        try:
            return list(api.items(path, **filters))
        except ApiError as error:
            errors.append({"path": path, "filters": filters, "error": str(error), "status": error.status})
            return None

    try:
        requester = api.request("GET", "/requester")
        requester_id, requester_balance = requester.get("id"), requester.get("balance")
    except ApiError as error:
        errors.append({"path": "/requester", "error": str(error), "status": error.status})
        requester_id = None
        requester_balance = None
    inventory = read("/projects") if include_other_projects else None
    project_ids = {str(row["id"]) for row in inventory or []}
    project_ids.update((str(project_id), str(reference_id)))
    records = []
    for current in sorted(project_ids, key=lambda value: (value not in {str(project_id), str(reference_id)}, value)):
        print(json.dumps({"reading_project": current}), flush=True)
        try:
            project = api.request("GET", "/projects/" + current)
        except ApiError as error:
            errors.append({"path": "/projects/" + current, "error": str(error), "status": error.status})
            continue
        spec = project.get("task_spec", {})
        script = spec.get("view_spec", {}).get("script", "")
        record = {
            "id": current, "public_name": project.get("public_name"), "status": project.get("status"),
            "owner": owner_fields(project),
            "task_spec": spec, "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "trainings": [], "pools": [],
        }
        trainings = read("/trainings", project_id=current)
        pools = read("/pools", project_id=current)
        record["training_inventory_complete"] = trainings is not None
        record["pool_inventory_complete"] = pools is not None
        for row in pools or []:
            record["pools"].append({
                "owner": owner_fields(row),
                **{
                key: row[key] for key in (
                    "id", "project_id", "private_name", "status", "type", "filter", "quality_control",
                    "mixer_config", "defaults", "training_config", "assignments_issuing_config",
                    "reward_per_assignment",
                ) if key in row
                },
            })
        for training in trainings or []:
            training_id = str(training["id"])
            try:
                tasks = list(islice(api.items("/tasks", pool_id=training_id), 501))
            except ApiError as error:
                errors.append({"path": "/tasks", "filters": {"pool_id": training_id},
                               "error": str(error), "status": error.status})
                tasks = None
            record["trainings"].append({
                "settings": {key: value for key, value in training.items() if key != "owner"},
                "tasks_complete": tasks is not None and len(tasks) <= 500,
                "tasks": None if tasks is None else [
                    {key: row[key] for key in (
                        "id", "pool_id", "input_values", "known_solutions", "baseline_solutions",
                        "message_on_unknown_solution", "overlap", "infinite_overlap",
                        "unavailable_for", "reserved_for",
                    ) if key in row}
                    for row in tasks[:500]
                ],
            })
        records.append(record)
    return scrub({
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "requester_id": requester_id,
        "requester_balance": requester_balance,
        "requested_project": str(project_id), "reference_project": str(reference_id),
        "include_other_projects": include_other_projects,
        "project_inventory_complete": inventory is not None,
        "inventory": None if inventory is None else [
            {key: row[key] for key in ("id", "public_name", "status") if key in row}
            for row in inventory
        ],
        "projects": records, "errors": errors,
    })


def requested_project_summaries(report):
    requested = {report["requested_project"], report["reference_project"]}
    result = []
    for project in report["projects"]:
        if project["id"] not in requested:
            continue
        owner = project["owner"]
        result.append({
            "id": project["id"], "status": project.get("status"), "owner": owner,
            "owner_matches_requester": owner_matches(owner, report["requester_id"]),
            "output_fields": sorted(project.get("task_spec", {}).get("output_spec", {})),
            "pools": [{
                "id": pool["id"], "status": pool.get("status"),
                "reward_per_assignment": pool.get("reward_per_assignment"),
                "owner": pool["owner"],
                "owner_matches_requester": owner_matches(pool["owner"], report["requester_id"]),
                "owner_matches_project": (
                    str(pool["owner"]["id"]) == str(owner["id"])
                    if pool["owner"].get("id") is not None and owner.get("id") is not None
                    else None
                ),
            } for pool in project["pools"]],
        })
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--only-requested", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-token", action="store_true")
    args = parser.parse_args()
    api = TasksApi(getpass.getpass("OAuth: ")) if args.prompt_token else TasksApi()
    reference = args.project if args.reference is None else args.reference
    report = collect(api, args.project, reference, include_other_projects=not args.only_requested)
    write_json(args.output, report)
    print(json.dumps({
        "report": str(args.output), "projects": len(report["projects"]),
        "reference_accessible": any(row["id"] == reference for row in report["projects"]),
        "requester_id": report["requester_id"], "requester_balance": report["requester_balance"],
        "requested_projects": requested_project_summaries(report),
        "errors": report["errors"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ApiError, ValueError) as error:
        raise SystemExit("Ошибка: " + str(error)) from None
