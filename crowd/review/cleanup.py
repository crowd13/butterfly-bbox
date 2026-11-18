import argparse
import getpass
import json
import sys
from pathlib import Path

from crowd.bbox.api import ApiError, TasksApi
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json

ACCOUNT = Path(__file__).resolve().parent / "runs/diagnostics/account.json"
PROJECTS = {
    key: "Проверьте рамки бабочек" for key in (
        "11842", "11843", "11886", "11887", "11890", "11898", "11899"
    )
}


def identity(row, expected, project=False):
    if project and expected.get("public_name") != "Проверьте рамки бабочек":
        raise ValueError("Cleanup only permits projects named Проверьте рамки бабочек")
    name = "public_name" if project else "private_name"
    if str(row.get("id")) != str(expected["id"]) or row.get(name) != expected[name]:
        raise ValueError(f"Cleanup resource identity changed: {expected['id']}")
    if not project and str(row.get("project_id")) != str(expected["project_id"]):
        raise ValueError(f"Cleanup resource belongs to another project: {expected['id']}")
    if "owner" in row and row["owner"].get("myself") is not True:
        raise ValueError(f"Cleanup resource is not owned by this account: {expected['id']}")


def transition(api, path, expected, action, journal, journal_path):
    url = f"{path}/{expected['id']}"
    target = "ARCHIVED" if action == "archive" else "CLOSED"
    key = url + "/" + action
    row = api.request("GET", url)
    identity(row, expected, path == "/projects")
    previous = journal.setdefault("operations", {}).get(key)
    if previous:
        api.wait(api.request("GET", "/operations/" + previous))
        row = api.request("GET", url)
        identity(row, expected, path == "/projects")
    if row["status"] == target:
        journal.setdefault("unresolved", {}).pop(key, None)
        write_json(journal_path, journal)
        return row
    if journal.setdefault("unresolved", {}).get(key):
        raise ValueError(f"Cleanup response is unresolved; check resource {expected['id']}")
    journal["unresolved"][key] = True
    write_json(journal_path, journal)
    error = None
    try:
        operation = api.request("POST", key)
        if operation.get("id"):
            journal["operations"][key] = operation["id"]
            journal["unresolved"].pop(key, None)
            write_json(journal_path, journal)
        api.wait(operation)
    except ApiError as caught:
        error = caught
        if caught.status is not None:
            journal["unresolved"].pop(key, None)
            write_json(journal_path, journal)
    row = api.request("GET", url)
    identity(row, expected, path == "/projects")
    if row["status"] != target:
        if error is not None:
            raise error
        raise ValueError(f"Cleanup {action} is not confirmed: {expected['id']}")
    journal["unresolved"].pop(key, None)
    journal.setdefault("readback", {})[url] = row
    write_json(journal_path, journal)
    return row


def archive_projects(api, directory, exclude_project_id=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = directory / "cleanup-plan.json"
    journal_path = directory / "cleanup-state.json"
    excluded = None if exclude_project_id is None else str(exclude_project_id)
    if plan_path.exists():
        plan = read_json(plan_path)
        if plan["exclude_project_id"] != excluded:
            raise ValueError("Cleanup exclusion differs from the frozen plan")
    else:
        account = read_json(ACCOUNT)
        if not account.get("project_inventory_complete") or account.get("errors"):
            raise ValueError("Cleanup requires a complete account inventory")
        rows = {str(row["id"]): row for row in account["projects"]}
        selected = []
        for project_id, name in PROJECTS.items():
            if project_id == excluded:
                continue
            row = rows[project_id]
            if row["public_name"] != name or not all(row.get(key) for key in (
                "training_inventory_complete", "pool_inventory_complete"
            )):
                raise ValueError(f"Cleanup inventory is incomplete or renamed: {project_id}")
            selected.append({
                "id": project_id, "public_name": name,
                "pools": row["pools"],
                "trainings": [item["settings"] for item in row["trainings"]],
            })
        plan = {"exclude_project_id": excluded, "projects": selected}
        write_json(plan_path, plan)
        write_json(directory / "account-before.json", account)
    journal = read_json(journal_path) if journal_path.exists() else {"archived": []}
    for project in plan["projects"]:
        if project["id"] == excluded or PROJECTS.get(project["id"]) != project["public_name"]:
            raise ValueError("Cleanup plan includes an unapproved project")
        remote_project = api.request("GET", "/projects/" + project["id"])
        identity(remote_project, project, True)
        snapshot = {"project": remote_project}
        for key in ("pools", "trainings"):
            children = list(api.items("/" + key, project_id=project["id"]))
            expected = {str(row["id"]): row for row in project[key]}
            if {str(row["id"]) for row in children} != set(expected):
                raise ValueError(f"Cleanup child inventory changed: {project['id']}")
            for child in children:
                identity(child, expected[str(child["id"])])
            snapshot[key] = children
        backup = directory / ("project-" + project["id"] + "-before.json")
        if not backup.exists():
            write_json(backup, snapshot)
        for key in ("pools", "trainings"):
            path = "/" + key
            for child in project[key]:
                current = api.request("GET", f"{path}/{child['id']}")
                identity(current, child)
                if current["status"] == "ARCHIVED":
                    continue
                if current["status"] == "OPEN":
                    transition(api, path, child, "close", journal, journal_path)
                elif current["status"] != "CLOSED":
                    raise ValueError(f"Cleanup cannot archive status {current['status']}")
                if any(True for _ in api.items(
                    "/assignments", pool_id=child["id"], status="ACTIVE,SUBMITTED"
                )):
                    raise ValueError(f"Unsettled assignments prevent archival: {child['id']}")
                transition(api, path, child, "archive", journal, journal_path)
        transition(api, "/projects", project, "archive", journal, journal_path)
        if project["id"] not in journal["archived"]:
            journal["archived"].append(project["id"])
            write_json(journal_path, journal)
    return {"archived": journal["archived"], "excluded": excluded, "journal": str(journal_path)}


def main():
    parser = argparse.ArgumentParser(description="Архивировать только старые проекты Проверьте рамки бабочек")
    parser.add_argument("--run", type=Path, default=ACCOUNT.parent.parent / "archive-review-2026-11-18")
    parser.add_argument("--prompt-token", action="store_true")
    args = parser.parse_args()
    api = TasksApi(getpass.getpass("OAuth: ")) if args.prompt_token else TasksApi()
    print(json.dumps(archive_projects(api, args.run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ApiError, ValueError) as error:
        print("Ошибка: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)
        raise SystemExit(130) from None
