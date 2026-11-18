import copy
import hashlib
import json
from pathlib import Path

from crowd.bbox.api import ApiError
from crowd.review.prepare import read_json
from crowd.review.verification import differences, normalize_project, verify_resources

PROJECT_FIELDS = {
    "public_name", "public_description", "public_instructions", "private_comment", "task_spec",
    "assignments_issuing_type", "assignments_issuing_view_config", "assignments_automerge_enabled",
    "max_active_assignments_count", "metadata", "sensitive", "quality_control",
    "localization_config",
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def writable_project(project):
    return {key: copy.deepcopy(value) for key, value in project.items() if key in PROJECT_FIELDS}


def project_matches(actual, expected, exact=False):
    actual = normalize_project(writable_project(actual))
    expected = normalize_project(writable_project(expected))
    return not differences(actual, expected) and (not exact or not differences(expected, actual))


def update_interface(api, state, directory, view_spec):
    directory = Path(directory)
    project_id = state["resources"]["project"]
    artifacts = {key: view_spec[key] for key in ("markup", "styles", "script")}
    revision = hashlib.sha256(
        json.dumps({"project_id": project_id, **artifacts}, sort_keys=True).encode()
    ).hexdigest()[:16]
    journal_path = directory / "interface-updates" / f"{revision}.json"
    remote = api.request("GET", f"/projects/{project_id}")
    if journal_path.exists():
        journal = read_json(journal_path)
        target = journal["after"]
        saved_view = target["task_spec"]["view_spec"]
        if journal["project_id"] != project_id or any(
            saved_view[key] != value for key, value in artifacts.items()
        ):
            raise ValueError("Interface update journal does not match this run")
    else:
        if not project_matches(remote, state["definitions"]["project"]):
            raise ValueError("Remote project changed; interface update stopped")
        before = writable_project(remote)
        target = copy.deepcopy(before)
        target["task_spec"]["view_spec"].update(artifacts)
        journal = {
            "project_id": project_id, "before": before, "after": target, "status": "prepared"
        }
    already_applied = project_matches(remote, target, exact=True)
    if not already_applied:
        if not project_matches(remote, journal["before"], exact=True):
            raise ValueError("Remote project changed; interface update stopped")
        other_state = {
            "resources": {
                key: value for key, value in state["resources"].items() if key != "project"
            },
            "definitions": state["definitions"],
        }
        statuses = verify_resources(api, other_state)
        pools = {"training", "exam", "pilot", "main"}
        if any(statuses.get(key) != "CLOSED" for key in pools):
            raise ValueError("Close training, exam, pilot and main before updating the interface")
        write_json(journal_path, journal)
        try:
            api.request("PUT", f"/projects/{project_id}", target)
        except ApiError:
            remote = api.request("GET", f"/projects/{project_id}")
            if not project_matches(remote, target, exact=True):
                raise
        else:
            remote = api.request("GET", f"/projects/{project_id}")
        if not project_matches(remote, target, exact=True):
            raise ValueError(f"Updated project differs from the prepared interface: {project_id}")
    snapshot_path = directory / "snapshot.json"
    snapshot = read_json(snapshot_path)
    snapshot["project"] = target
    state["definitions"]["project"] = target
    write_json(directory / "state.json", state)
    write_json(snapshot_path, snapshot)
    journal["status"] = "applied"
    write_json(journal_path, journal)
    return {"project": project_id, "revision": revision, "updated": not already_applied}
