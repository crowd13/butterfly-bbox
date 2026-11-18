import copy
from pathlib import Path

from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json


def requester(api):
    row = api.request("GET", "/requester")
    identity = row.get("id")
    if not isinstance(identity, str) or not identity:
        raise ValueError("Requester response has no account ID")
    return row


def owned_project(api, project_id, requester_id):
    project = api.request("GET", "/projects/" + str(project_id))
    owner = project.get("owner", {})
    if project.get("id") != str(project_id) or (
        owner.get("id") != requester_id or owner.get("myself") is not True
    ):
        raise ValueError(f"Project {project_id} is not owned by the current requester")
    return project


def verify_bound_account(api, state):
    expected = state.get("requester_id")
    if expected is None:
        return None
    current = requester(api)
    if current["id"] != expected:
        raise ValueError("OAuth account differs from this run's bound requester")
    return {"requester_id": current["id"], "balance": current.get("balance")}


def bind_account(api, state, directory, source_project_id, source_assignments_path):
    source_project_id = str(source_project_id)
    if state.get("source_project_id") not in {None, source_project_id}:
        raise ValueError("Source project differs from this run's bound source")
    current = requester(api)
    requester_id = current["id"]
    if state.get("requester_id") not in {None, requester_id}:
        raise ValueError("OAuth account differs from this run's bound requester")
    owned_project(api, source_project_id, requester_id)
    assignments = read_json(source_assignments_path)
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("Source assignments must be a nonempty frozen list")
    owners = {row.get("owner", {}).get("id") for row in assignments}
    if owners != {requester_id}:
        raise ValueError("Source assignment owner differs from the current requester")
    pool_ids = {row.get("pool_id") for row in assignments}
    if any(not isinstance(pool_id, str) or not pool_id for pool_id in pool_ids):
        raise ValueError("Source assignments contain an invalid pool ID")
    for pool_id in sorted(pool_ids):
        pool = api.request("GET", "/pools/" + pool_id)
        if pool.get("id") != pool_id or pool.get("project_id") != source_project_id:
            raise ValueError(
                f"Source pool {pool_id} does not belong to project {source_project_id}"
            )
    review_project = state.get("resources", {}).get("project")
    if review_project is not None:
        owned_project(api, review_project, requester_id)
    updated = copy.deepcopy(state)
    updated.update(requester_id=requester_id, source_project_id=source_project_id)
    write_json(Path(directory) / "state.json", updated)
    state.clear()
    state.update(updated)
    return {
        "requester_id": requester_id, "source_project_id": source_project_id,
        "source_pools": sorted(pool_ids), "balance": current.get("balance"),
    }
