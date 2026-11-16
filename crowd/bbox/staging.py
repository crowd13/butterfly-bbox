"""Upload closed training/work pools; activate work only through next-batch."""

import html
import uuid

from .api import ApiError
from .budget import check_commitment
from .render import public_image


def create_training(run):
    marker = "Обучение"
    training_id = run.ensure_resource(
        "training",
        "/trainings",
        {
            "project_id": run.state["resources"]["project"],
            "private_name": marker,
            "may_contain_adult_content": False,
            "assignment_max_duration_seconds": run.config["assignment_seconds"],
            "mix_tasks_in_creation_order": True,
            "shuffle_tasks_in_task_suite": False,
            "training_tasks_in_task_suite_count": 1,
            "task_suites_required_to_pass": run.config["practice_size"],
            "inherited_instructions": True,
        },
        "private_name",
        marker,
    )
    training = run.state.setdefault(
        "training", {"operation_id": str(uuid.uuid4()), "uploaded": False}
    )
    run.save()
    if training["uploaded"]:
        return
    tasks = [
        {
            "pool_id": training_id,
            "input_values": public_image(row),
            "known_solutions": [{"output_values": row["solution"], "correctness_weight": 1.0}],
            "message_on_unknown_solution": html.escape(row["hint"]),
            "infinite_overlap": True,
        }
        for row in run.snapshot["bank"]["practice"]
    ]
    try:
        operation = run.api.request("GET", "/operations/" + training["operation_id"])
    except ApiError as exc:
        if exc.status != 404:
            raise
        operation = run.api.request(
            "POST",
            "/tasks",
            tasks,
            query={
                "async_mode": "true",
                "operation_id": training["operation_id"],
                "skip_invalid_items": "false",
                "open_pool": "false",
            },
        )
    run.api.wait(operation)
    remote = [
        task
        for task in run.api.items("/tasks", pool_id=training_id)
        if task["id"] not in training.get("retired_task_ids", [])
    ]
    expected = {row["input_values"]["photo_id"]: row for row in tasks}
    if len(remote) != len(expected):
        raise ValueError("Training task count differs")
    for row in remote:
        original = expected[row["input_values"]["photo_id"]]
        if (
            row["input_values"] != original["input_values"]
            or row["known_solutions"] != original["known_solutions"]
        ):
            raise ValueError("Training content differs")
    training["uploaded"] = True
    run.save()


def stage(run):
    """Preload both work pools without altering the current exam/pilot lifecycle."""
    run.create()
    staged = run.state.setdefault("staged_batches", [])
    all_batches = run.state["batches"] + staged
    manifest = run.snapshot["manifest"]
    for index, (kind, rows) in enumerate(
        (
            ("pilot", manifest[: run.config["pilot_size"]]),
            ("main", manifest[run.config["pilot_size"] :]),
        ),
        1,
    ):
        if any(b["kind"] == kind for b in all_batches):
            continue
        batch = run.build_batch(rows, kind, None, [], index=index)
        # Empty reservations are populated from actual exam results before activation.
        batch["staged"] = True
        check_commitment(all_batches + [batch], run.config)
        staged.append(batch)
        all_batches.append(batch)
        run.save()
    for batch in staged:
        run.upload(batch)


def activate_staged(run, batch, workers, assignments):
    """Populate eligibility from real exposure history, then select the existing pool."""
    pool = run.api.request("GET", "/pools/" + batch["pool_id"])
    if pool["status"] != "CLOSED" or list(run.api.items("/assignments", pool_id=batch["pool_id"])):
        raise ValueError("Staged pool must be closed and never issued")
    seen = {}
    for assignment in assignments:
        if not assignment.get("tasks"):
            raise ValueError("Cannot establish exposure history")
        for task in assignment["tasks"]:
            seen.setdefault(str(task["input_values"]["photo_id"]), set()).add(assignment["user_id"])
    controls = {str(r["photo_id"]) for r in run.snapshot["bank"]["controls"]}
    updates = []
    for suite in batch["suites"]:
        control = next(
            str(t["input_values"]["photo_id"])
            for t in suite["tasks"]
            if str(t["input_values"]["photo_id"]) in controls
        )
        excluded = seen.get(control, set())
        eligible = workers - excluded
        if not eligible:
            raise ValueError(
                "Staged control has no eligible worker; replenish workers before activation"
            )
        updates.append(
            (suite, {"reserved_for": sorted(eligible), "unavailable_for": sorted(excluded)})
        )
    # The API silently ignores reservation changes in PATCH. Retire preview pages
    # and upload replacements with real eligibility, keeping the same closed pool.
    if not batch.get("retired_suite_ids"):
        batch["retired_suite_ids"] = [s.pop("remote_id") for s, _ in updates]
        for suite, update in updates:
            suite.update(update)
        batch["operation_id"] = str(uuid.uuid4())
        batch["uploaded"] = False
        run.save()
    for suite_id in batch["retired_suite_ids"]:
        run.api.request(
            "PATCH",
            "/task-suites/" + suite_id,
            {"overlap": 0, "infinite_overlap": False},
            query={"open_pool": "false"},
        )
    run.upload(batch)
    run.verify_remote(batch)
    batch.pop("staged", None)
    run.state["staged_batches"].remove(batch)
    run.state["batches"].append(batch)
    run.save()
