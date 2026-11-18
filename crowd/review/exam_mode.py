import copy
from pathlib import Path

from crowd.bbox.api import ApiError
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json
from crowd.review.verification import differences

POOL_FIELDS = {
    "project_id", "private_name", "private_comment", "public_description",
    "public_instructions", "may_contain_adult_content", "will_expire",
    "auto_close_after_complete_delay_seconds", "reward_per_assignment",
    "dynamic_pricing_config", "assignment_max_duration_seconds", "auto_accept_solutions",
    "auto_accept_period_day", "assignments_issuing_config", "filter", "quality_control",
    "mixer_config", "dynamic_overlap_config", "training_config", "defaults", "training",
    "priority", "speed_quality_balance", "metadata",
}


def writable_pool(pool):
    return {key: copy.deepcopy(value) for key, value in pool.items() if key in POOL_FIELDS}


def pool_differences(actual, expected):
    return differences(actual, expected) + differences(expected, actual)


def _set_requirement(pool, requirement):
    if requirement is None:
        pool["quality_control"].pop("training_requirement", None)
    else:
        pool["quality_control"]["training_requirement"] = copy.deepcopy(requirement)


def _use_mode(api, state, directory, mode, requirement, journal_name):
    directory = Path(directory)
    journal_path = directory / journal_name
    resources = state["resources"]
    pool_id = resources["exam"]
    remote = {}
    for key in ("training", "exam", "pilot", "main"):
        path = "/trainings" if key == "training" else "/pools"
        row = api.request("GET", f"{path}/{resources[key]}")
        if row.get("status") != "CLOSED":
            raise ValueError(f"Close {key} before changing the exam admission mode")
        if row.get("project_id") != resources["project"]:
            raise ValueError(f"Resource belongs to another project: {key}")
        remote[key] = row
    for key in ("exam", "pilot", "main"):
        if any(True for _ in api.items("/assignments", pool_id=resources[key])):
            raise ValueError(f"Cannot change admission after assignments exist: {key}")
    for key in ("training", "pilot", "main"):
        expected = copy.deepcopy(state["definitions"][key])
        expected.pop("will_expire", None)
        if differences(remote[key], expected):
            raise ValueError(f"Remote settings changed: {key}")

    current = writable_pool(remote["exam"])
    if journal_path.exists():
        journal = read_json(journal_path)
        if journal["project_id"] != resources["project"] or journal["exam"] != pool_id:
            raise ValueError("Exam mode journal belongs to another run")
        before, target = journal["before"], journal["after"]
        intended = copy.deepcopy(before)
        _set_requirement(intended, requirement)
        if intended != target:
            raise ValueError("Exam mode journal changes more than native training admission")
    else:
        expected = copy.deepcopy(state["definitions"]["exam"])
        expected.pop("will_expire", None)
        if differences(current, expected) or (
            current["quality_control"].get("training_requirement")
            != expected["quality_control"].get("training_requirement")
        ):
            raise ValueError("Remote exam settings changed; migration stopped")
        before = current
        target = copy.deepcopy(before)
        _set_requirement(target, requirement)
        journal = {
            "project_id": resources["project"], "exam": pool_id,
            "before": before, "after": target, "status": "prepared",
        }

    already_applied = not pool_differences(current, target)
    if not already_applied and pool_differences(current, before):
        raise ValueError("Remote exam changed outside the prepared migration")
    write_json(journal_path, journal)
    if not already_applied:
        error = None
        try:
            api.request("PUT", f"/pools/{pool_id}", target)
        except ApiError as caught:
            error = caught
        row = api.request("GET", f"/pools/{pool_id}")
        current = writable_pool(row)
        journal["readback"] = current
        journal["differences"] = pool_differences(current, target)
        if row.get("status") != "CLOSED" or journal["differences"]:
            journal["status"] = "unconfirmed"
            write_json(journal_path, journal)
            if error is not None:
                raise error
            raise ValueError("Exam admission update is not confirmed by readback")

    updated_state = copy.deepcopy(state)
    updated_state["definitions"]["exam"] = target
    updated_state["training_mode"] = mode
    write_json(directory / "state.json", updated_state)
    state.clear()
    state.update(updated_state)
    journal.update(status="applied", readback=current, differences=[])
    write_json(journal_path, journal)
    return {
        "exam": pool_id, "training_mode": mode, "updated": not already_applied,
        "journal": str(journal_path),
    }


def use_exam(api, state, directory):
    return _use_mode(api, state, directory, "inline", None, "exam-mode.json")


def use_native(api, state, directory):
    requirement = {
        "training_pool_id": state["resources"]["training"],
        "training_passing_skill_value": 0,
    }
    return _use_mode(api, state, directory, "native", requirement, "native-mode.json")
