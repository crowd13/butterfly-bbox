"""Exam, pilot and main pools; optional repeats. No background scheduler."""

import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import ApiError
from .budget import check_commitment, committed, cost, money, plan
from .dataset import image_preflight, validate_inputs
from .export import TERMINAL_ANSWERS, answers, coverage
from .quality import control_windows, evaluate, match_count, validate_solution
from .render import assets, instruction_structure, project_body, public_image
from .storage import digest, read_json, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def prepare(
    directory, manifest, bank, config, *, test_only=False, draft_gold=False, with_training=False
):
    path = Path(directory) / "state.json"
    if path.exists():
        raise ValueError("Run already exists; keep its inputs immutable")
    validate_inputs(manifest, bank, config, require_reviews=not (test_only or draft_gold))
    snapshot = {
        "manifest": manifest,
        "bank": bank,
        "config": config,
        "assets": assets(bank, config["iou_threshold"]),
        "manifest_hash": digest(manifest),
        "test_only": test_only,
        "native_training": with_training,
    }
    snapshot["instruction_hash"] = digest(snapshot["assets"])
    write_json(Path(directory) / "inputs.json", snapshot)
    write_json(
        path,
        {
            "run_id": uuid.uuid4().hex[:12],
            "input_hash": digest(snapshot),
            "resources": {},
            "pending": {},
            "batches": [],
            "workers": {},
            "events": {},
            "exam_scores": {},
            "audits": {},
            "adjudications": {},
            "resets": {},
            "restore_history": [],
            "releases": {},
            "pilot": None,
            "readiness": None,
            "preflight": None,
        },
    )


class Run:
    def __init__(self, directory, api=None):
        self.directory = Path(directory)
        self.snapshot = read_json(self.directory / "inputs.json")
        self.state = read_json(self.directory / "state.json")
        if digest(self.snapshot) != self.state["input_hash"]:
            raise ValueError("Frozen inputs changed; restore them or prepare a new run")
        self.config = self.snapshot["config"]
        self.api = api
        self.gold = {str(r["photo_id"]): r for rows in self.snapshot["bank"].values() for r in rows}

    def save(self):
        write_json(self.directory / "state.json", self.state)

    def preflight(self):
        rows = self.snapshot["manifest"] + list(self.gold.values())
        report = {
            "input_hash": self.state["input_hash"],
            "checked_at": now(),
            "images": image_preflight(rows),
        }
        write_json(self.directory / "preflight.json", report)
        self.state["preflight"] = {"checked_at": report["checked_at"], "hash": digest(report)}
        self.save()

    def ready(self, record):
        self.require_production()
        validate_inputs(self.snapshot["manifest"], self.snapshot["bank"], self.config)
        if record.get("input_hash") != self.state["input_hash"]:
            raise ValueError("Readiness must refer to this run's input_hash")
        for field in ("interface_checked", "visual_duplicates_checked", "gold_reviewed"):
            if record.get(field) is not True:
                raise ValueError(f"Confirm {field} after actual human review")
        if money(record.get("commission", "-1")) != money(self.config["commission"]):
            raise ValueError("Actual commission differs; replan before opening")
        if not record.get("reviewer", "").strip() or not record.get("reason", "").strip():
            raise ValueError("Name the reviewer and record what was checked")
        self.state["readiness"] = {**record, "recorded_at": now()}
        self.save()

    def require_production(self):
        if self.snapshot.get("test_only"):
            raise ValueError("Test-only run: moderation and paid launch are disabled")

    def ensure_resource(self, key, path, payload, marker_field, marker):
        if key in self.state["resources"]:
            return self.state["resources"][key]
        # Reconcile only our own stable marker before retrying creation.
        markers = {marker}
        if marker_field == "private_name":
            markers.add("bbox:" + self.state["run_id"] + ":" + key)
        matches = [
            x
            for x in self.api.items(path)
            if x.get(marker_field) in markers
            and ("project_id" not in payload or x.get("project_id") == payload["project_id"])
        ]
        if len(matches) > 1:
            raise ValueError(f"Duplicate resource marker {marker}; reconcile manually")
        if matches:
            resource = matches[0]
        else:
            if key in self.state["pending"]:
                raise ValueError(f"Creation of {key} is uncertain. Retry later; do not POST again")
            self.state["pending"][key] = digest(payload)
            self.save()
            try:
                resource = self.api.request("POST", path, payload)
            except ApiError as exc:
                if exc.status and 400 <= exc.status < 500 and exc.status not in {408, 409, 429}:
                    self.state["pending"].pop(key)
                    self.save()
                raise
        self.state["resources"][key] = resource["id"]
        self.state["pending"].pop(key, None)
        self.save()
        return resource["id"]

    def create(self):
        prefix = "bbox:" + self.state["run_id"]
        body = project_body(self.snapshot, prefix)
        if self.snapshot.get("test_only"):
            body["public_name"] = "ТЕСТ — " + body["public_name"]
            body["public_instructions"] = (
                "<p><strong>Тестовый проект. Черновые эталоны ИИ; "
                "не для платной выдачи.</strong></p>" + body["public_instructions"]
            )
        self.ensure_resource("project", "/projects", body, "private_comment", prefix)
        for name in ("exam", "access"):
            marker = prefix + ":" + name
            self.ensure_resource(
                name,
                "/skills",
                {
                    # The API requires name to match public_name.EN.
                    "name": "Butterflies: " + name,
                    "private_comment": marker,
                    "hidden": name == "access",
                    "public_name": {"RU": "Бабочки: " + name, "EN": "Butterflies: " + name},
                    "public_requester_description": {
                        "RU": "Допуск по проверенной разметке",
                        "EN": "Qualification for annotation",
                    },
                },
                "private_comment",
                marker,
            )
        if self.snapshot.get("native_training"):
            from .staging import create_training

            create_training(self)
        if not self.state["batches"]:
            suites = [
                {
                    "tasks": [
                        {"input_values": public_image(r)} for r in self.snapshot["bank"]["exam"]
                    ],
                    "overlap": self.config["exam_attempts"],
                    "infinite_overlap": False,
                }
            ]
            self.state["batches"].append(
                {
                    "key": "exam-pool",
                    "kind": "exam",
                    "suites": suites,
                    "price": self.config["exam_price"],
                    "overlap": self.config["exam_attempts"],
                    "operation_id": str(uuid.uuid4()),
                    "uploaded": False,
                    "work_ids": [],
                }
            )
            check_commitment(self.state["batches"], self.config)
            self.save()
        self.upload(self.state["batches"][-1])

    def pool_name(self, batch):
        title = {
            "exam": "Экзамен",
            "pilot": "Пилот",
            "main": "Основной",
            "repeat": "Повторная разметка",
        }[batch["kind"]]
        suffix = f" №{int(batch['key'].split('-')[-1])}" if batch["kind"] == "repeat" else ""
        return title + suffix

    def pool_body(self, batch):
        filters = [{"category": "profile", "key": "languages", "operator": "IN", "value": "RU"}]
        if batch["kind"] != "exam":
            filters += [
                {
                    "category": "skill",
                    "key": self.state["resources"]["exam"],
                    "operator": "GTE",
                    "value": self.config["exam_pass"],
                },
                {
                    "category": "skill",
                    "key": self.state["resources"]["access"],
                    "operator": "EQ",
                    "value": 100,
                },
            ]
        body = {
            "project_id": self.state["resources"]["project"],
            "private_name": self.pool_name(batch),
            "will_expire": (datetime.now(timezone.utc) + timedelta(days=14)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "reward_per_assignment": float(batch["price"]),
            "assignment_max_duration_seconds": self.config["assignment_seconds"],
            "auto_accept_solutions": True,
            "auto_close_after_complete_delay_seconds": 0,
            "may_contain_adult_content": False,
            # The requester UI expects AND groups containing OR groups.
            "filter": {"and": [{"or": [item]} for item in filters]},
            "defaults": {"default_overlap_for_new_task_suites": batch["overlap"]},
        }

        if batch["kind"] == "exam" and self.snapshot.get("native_training"):
            body["quality_control"] = {
                "training_requirement": {
                    "training_pool_id": self.state["resources"]["training"],
                    "training_passing_skill_value": 0,
                }
            }
        return body

    def upload(self, batch):
        payload = self.pool_body(batch)
        pool_id = self.ensure_resource(
            batch["key"], "/pools", payload, "private_name", payload["private_name"]
        )
        batch["pool_id"] = pool_id
        self.save()
        if batch["uploaded"]:
            return
        try:
            operation = self.api.request("GET", f"/operations/{batch['operation_id']}")
        except ApiError as exc:
            if exc.status != 404:
                raise
            operation = self.api.request(
                "POST",
                "/task-suites",
                [{**suite, "pool_id": pool_id} for suite in batch["suites"]],
                query={
                    "async_mode": "true",
                    "operation_id": batch["operation_id"],
                    "skip_invalid_items": "false",
                    "open_pool": "false",
                },
            )
        self.api.wait(operation)
        remote = [
            s
            for s in self.api.items("/task-suites", pool_id=pool_id)
            if s["id"] not in batch.get("retired_suite_ids", [])
        ]

        def signature(suite):
            return digest([t["input_values"] for t in suite["tasks"]])

        expected = {signature(s): s for s in batch["suites"]}
        if len(remote) != len(expected) or {signature(s) for s in remote} != set(expected):
            raise ValueError("Uploaded task suites do not match the local batch")
        for suite in remote:
            expected[signature(suite)]["remote_id"] = suite["id"]
        batch["uploaded"] = True
        self.save()

    def all_batches(self):
        return self.state["batches"] + self.state.get("staged_batches", [])

    def pool_progress(self, assignments):
        reports = []
        for batch in self.all_batches():
            if not batch.get("pool_id"):
                continue
            pool = self.api.request("GET", f"/pools/{batch['pool_id']}")
            rows = [a for a in assignments if a["pool_id"] == batch["pool_id"]]
            accepted = Counter(a["task_suite_id"] for a in rows if a["status"] == "ACCEPTED")
            expected = {s.get("remote_id") for s in batch["suites"]}
            settled = not any(a["status"] in {"ACTIVE", "SUBMITTED"} for a in rows)
            complete = (
                batch["uploaded"]
                and pool["status"] == "CLOSED"
                and settled
                and all(accepted[sid] == batch["overlap"] for sid in expected)
                and not (accepted.keys() - expected)
            )
            reports.append(
                {
                    "pool_id": batch["pool_id"],
                    "kind": batch["kind"],
                    "status": pool["status"],
                    "assignments": dict(Counter(a["status"] for a in rows)),
                    "completed_pages": sum(accepted[sid] == batch["overlap"] for sid in expected),
                    "expected_pages": len(expected),
                    "complete": bool(complete),
                }
            )
        return reports

    def sync(self):
        items = []
        for batch in self.all_batches():
            if batch.get("pool_id"):
                items.extend(self.api.items("/assignments", pool_id=batch["pool_id"]))
        write_json(self.directory / "assignments.json", items)
        return items

    def closed_complete(self, batch, assignments):
        rows = [a for a in assignments if a["pool_id"] == batch["pool_id"]]
        if any(a["status"] in {"ACTIVE", "SUBMITTED"} for a in rows):
            raise ValueError("Wait for active/submitted assignments to settle")
        if batch["kind"] != "exam":
            for suite in batch["suites"]:
                accepted = [
                    a
                    for a in rows
                    if a["task_suite_id"] == suite["remote_id"] and a["status"] == "ACCEPTED"
                ]
                if len(accepted) != 1:
                    raise ValueError(
                        "Batch incomplete or duplicate paid assignments; reconcile first"
                    )
        pool = self.api.request("GET", f"/pools/{batch['pool_id']}")
        if pool["status"] == "OPEN":
            raise ValueError("Close the pool before scoring/releasing it")
        return rows

    def set_access(self, worker, value):
        self.api.skill(self.state["resources"]["access"], worker, value)
        self.state["workers"].setdefault(worker, {})["access"] = value
        self.save()

    def score_exam(self, assignments=None):
        assignments = self.sync() if assignments is None else assignments
        batch = self.state["batches"][0]
        rows = answers(assignments, self.snapshot)
        for a in assignments:
            if a["pool_id"] != batch["pool_id"] or a["status"] != "ACCEPTED":
                continue
            worker = a["user_id"]
            if any(
                v["worker_id"] == worker and key != a["id"]
                for key, v in self.state["exam_scores"].items()
            ):
                raise ValueError("Worker has multiple exam attempts; reconcile")
            exam = [r for r in rows if r["assignment_id"] == a["id"]]
            results = [self.grade(r) for r in exam]
            previous = self.state["exam_scores"].get(a["id"])
            if previous and previous["results"] == results:
                continue
            score = 100 * sum(v["correct"] is True for v in results) / self.config["exam_size"]
            passed = (
                len(exam) == self.config["exam_size"]
                and score >= self.config["exam_pass"]
                and all(v["correct"] is not None for v in results)
            )
            self.api.skill(self.state["resources"]["exam"], worker, score)
            self.set_access(
                worker,
                100 if passed and not self.state["workers"].get(worker, {}).get("failed") else 0,
            )
            self.state["exam_scores"][a["id"]] = {
                "worker_id": worker,
                "score": score,
                "passed": passed,
                "results": results,
            }
            self.save()
        return self.state["exam_scores"]

    def grade(self, row):
        override = self.state["adjudications"].get(row["answer_id"])
        if override:
            return {"correct": override["correct"], "reason": override["reason"]}
        return evaluate(
            row["solution"], self.gold[row["photo_id"]]["solution"], self.config["iou_threshold"]
        )

    def score_controls(self, assignments=None, batch=None):
        assignments = self.sync() if assignments is None else assignments
        batch = self.state["batches"][-1] if batch is None else batch
        if batch["kind"] == "exam":
            raise ValueError("No work batch")
        if not batch["uploaded"]:
            raise ValueError("Finish uploading the pool before scoring controls")
        rows = answers(assignments, self.snapshot)
        first_seen = {}
        for assignment in sorted(assignments, key=lambda a: (a.get("created", ""), a["id"])):
            for task in assignment.get("tasks") or []:
                key = (assignment["user_id"], str(task["input_values"]["photo_id"]))
                first_seen.setdefault(key, assignment["id"])
        for row in rows:
            if row["role"] == "controls" and row["assignment_status"] == "ACCEPTED":
                grade = self.grade(row)
                if first_seen[row["worker_id"], row["photo_id"]] != row["assignment_id"]:
                    grade = {"correct": None, "reason": "Repeated control: human audit required"}
                self.state["events"][row["answer_id"]] = {
                    "id": row["answer_id"],
                    "worker_id": row["worker_id"],
                    "submitted": row["submitted"],
                    "pool_id": row["pool_id"],
                    **grade,
                }
        workers = {r["worker_id"] for r in rows if r["pool_id"] == batch["pool_id"]}
        report = {}
        for worker in workers:
            ignored = set(self.state["resets"].get(worker, {}).get("event_ids", []))
            events = [
                e
                for eid, e in self.state["events"].items()
                if e["worker_id"] == worker and eid not in ignored
            ]
            result = control_windows(
                events, self.config["control_window"], self.config["control_min_correct"]
            )
            was_failed = self.state["workers"].get(worker, {}).get("failed", False)
            result["failed"] |= was_failed
            if result["failed"]:
                if self.state["workers"].get(worker, {}).get("access") != 0:
                    self.set_access(worker, 0)
                self.state["workers"][worker]["failed"] = True
            required = (
                [
                    r["answer_id"]
                    for r in rows
                    if r["role"] == "work"
                    and r["pool_id"] == batch["pool_id"]
                    and r["worker_id"] == worker
                    and r["assignment_status"] == "ACCEPTED"
                ]
                if result["failed"] or result["needs_audit"]
                else []
            )
            result["required_audits"] = required
            report[worker] = result
        batch["control_report"] = report
        batch["scored_hash"] = digest([a for a in assignments if a["pool_id"] == batch["pool_id"]])
        self.save()
        return report

    def import_audit(self, records):
        rows = {r["answer_id"]: r for r in answers(self.sync(), self.snapshot)}
        batch = self.state["batches"][-1]
        for record in records:
            row = rows.get(record["answer_id"])
            if (
                not row
                or not record.get("reviewer", "").strip()
                or not record.get("reason", "").strip()
            ):
                raise ValueError("Audit needs an existing answer_id, reviewer and reason")
            if row["pool_id"] != batch["pool_id"]:
                raise ValueError(
                    "Only the latest batch can be adjudicated; older decisions are frozen"
                )
            if row["assignment_status"] != "ACCEPTED":
                raise ValueError("Audit only accepted submissions")
            if row["role"] == "work":
                validate_solution(record["solution"])
                if record["solution"]["status"] not in {"boxes", "no_butterflies"}:
                    raise ValueError("Human reference must resolve the image")
                self.state["audits"][record["answer_id"]] = record
            else:
                if type(record.get("correct")) not in (bool, type(None)) or "correct" not in record:
                    raise ValueError("Adjudication needs correct: true/false/null")
                self.state["adjudications"][record["answer_id"]] = record
        batch.pop("scored_hash", None)
        batch.pop("released", None)
        if batch["kind"] == "pilot":
            self.state["pilot"] = None
        self.save()

    def release(self, reviewer, reason):
        if not reviewer.strip() or not reason.strip():
            raise ValueError("Record reviewer and release reason")
        batch = self.state["batches"][-1]
        assignments = self.sync()
        self.closed_complete(batch, assignments)
        current = [a for a in assignments if a["pool_id"] == batch["pool_id"]]
        if batch.get("scored_hash") != digest(current):
            raise ValueError("Run score-controls after the latest answers")
        required = {
            x for report in batch["control_report"].values() for x in report["required_audits"]
        }
        unresolved = [
            r["answer_id"]
            for r in answers(assignments, self.snapshot)
            if r["pool_id"] == batch["pool_id"]
            and r["role"] == "controls"
            and r["assignment_status"] == "ACCEPTED"
            and self.grade(r)["correct"] is None
            and r["answer_id"] not in self.state["adjudications"]
        ]
        if unresolved:
            raise ValueError(f"Investigate control load errors and record decisions: {unresolved}")
        rows = [
            r
            for r in answers(assignments, self.snapshot)
            if r["pool_id"] == batch["pool_id"]
            and r["role"] == "work"
            and r["assignment_status"] == "ACCEPTED"
        ]
        if batch["kind"] == "pilot":
            required.update(r["answer_id"] for r in rows)
        missing = required - self.state["audits"].keys()
        if missing:
            raise ValueError(f"Human audit required for {len(missing)} answers; export the queue")
        if batch["kind"] == "pilot":
            correct = sum(
                evaluate(
                    r["solution"],
                    self.state["audits"][r["answer_id"]]["solution"],
                    self.config["iou_threshold"],
                )["correct"]
                is True
                for r in rows
            )
            metrics = {}
            for threshold in (0.5, 0.75):
                tp = fp = fn = 0
                for row in rows:
                    predicted = (
                        row["solution"]["boxes"]
                        if not row["validation_error"]
                        and row["solution"]["status"] in {"boxes", "no_butterflies"}
                        else []
                    )
                    gold = self.state["audits"][row["answer_id"]]["solution"]["boxes"]
                    matched = match_count(predicted, gold, threshold)
                    tp += matched
                    fp += len(predicted) - matched
                    fn += len(gold) - matched
                metrics[str(threshold)] = {
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
                }
            passed = (
                len(rows) == self.config["pilot_size"]
                and correct >= self.config["pilot_min_correct"]
            )
            self.state["pilot"] = {
                "passed": passed,
                "correct_images": correct,
                "total_images": len(rows),
                "metrics": metrics,
                "assignment_seconds": [
                    (
                        datetime.fromisoformat(a["submitted"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(a["created"].replace("Z", "+00:00"))
                    ).total_seconds()
                    for a in current
                    if a.get("submitted") and a.get("created")
                ],
                "qualified_workers": sum(
                    w.get("access") == 100 for w in self.state["workers"].values()
                ),
                "reviewer": reviewer,
                "reason": reason,
                "paid": str(cost(batch["price"], len(batch["suites"]), self.config["commission"])),
            }
        batch["released"] = {"reviewer": reviewer, "reason": reason, "at": now()}
        self.save()

    def restore(self, worker, reviewer, reason):
        if not reviewer.strip() or not reason.strip() or worker not in self.state["workers"]:
            raise ValueError("Restore needs a known worker, reviewer and reason")
        batch = self.state["batches"][-1]
        if not batch.get("released"):
            raise ValueError("Audit and release the current batch before restoring access")
        if not any(
            s["worker_id"] == worker and s["passed"] for s in self.state["exam_scores"].values()
        ):
            raise ValueError("Cannot restore a worker who did not pass the exam")
        self.state["resets"][worker] = {
            "event_ids": [
                eid for eid, e in self.state["events"].items() if e["worker_id"] == worker
            ],
            "reviewer": reviewer,
            "reason": reason,
            "at": now(),
        }
        self.state.setdefault("restore_history", []).append(
            {"worker_id": worker, **self.state["resets"][worker]}
        )
        self.state["workers"][worker]["failed"] = False
        self.set_access(worker, 100)
        self.save()

    def next_batch(self, repeat_ids=None, repeat_reason=None):
        assignments = self.sync()
        previous = self.state["batches"][-1]
        if not previous["uploaded"]:
            self.upload(previous)
            return
        self.closed_complete(previous, assignments)
        if previous["kind"] != "exam" and not previous.get("released"):
            raise ValueError("Score and release the previous batch first")
        workers = {w for w, v in self.state["workers"].items() if v.get("access") == 100}
        if not workers:
            raise ValueError("No qualified workers; score-exam first")
        used = {
            pid for b in self.state["batches"] if b["kind"] != "repeat" for pid in b["work_ids"]
        }
        manifest = self.snapshot["manifest"]
        if repeat_ids is not None:
            if not repeat_reason or not repeat_reason.strip():
                raise ValueError("Record a reason for the independent repeat")
            repeat_ids = [str(pid) for pid in repeat_ids]
            if len(set(repeat_ids)) != len(repeat_ids) or not set(repeat_ids) <= used:
                raise ValueError("Repeat IDs must be distinct previously issued work images")
            repeated = sum(
                len(b["work_ids"]) for b in self.state["batches"] if b["kind"] == "repeat"
            )
            if repeated + len(repeat_ids) > self.config["repeat_limit"]:
                raise ValueError("Repeat image limit exceeded")
            rows = [r for r in manifest if str(r["photo_id"]) in repeat_ids]
            kind = "repeat"
        else:
            if any(b["kind"] == "main" for b in self.state["batches"]):
                raise ValueError("Both work pools already exist; use repeats only when needed")
            if not any(b["kind"] == "pilot" for b in self.state["batches"]):
                rows = manifest[: self.config["pilot_size"]]
                kind = "pilot"
            else:
                if not self.state["pilot"] or not self.state["pilot"]["passed"]:
                    raise ValueError("Pilot failed; resolve the cause and replan before main work")
                rows = [r for r in manifest if str(r["photo_id"]) not in used]
                kind = "main"
        if not rows:
            raise ValueError("No images to issue")
        if kind == "main" and len(workers) < self.config["min_workers"]:
            raise ValueError("At least four qualified workers required for main annotation")
        staged = self.state.get("staged_batches", [])
        existing = next((b for b in staged if b["kind"] == kind), None)
        if existing:
            from .staging import activate_staged

            activate_staged(self, existing, workers, assignments)
            return
        batch = self.build_batch(rows, kind, workers, assignments, repeat_reason)
        check_commitment(
            self.state["batches"] + self.state.get("staged_batches", []) + [batch], self.config
        )
        self.state["batches"].append(batch)
        self.save()
        self.upload(batch)

    def build_batch(self, rows, kind, workers, assignments, repeat_reason=None, index=None):
        index = (
            len(self.state["batches"]) + len(self.state.get("staged_batches", []))
            if index is None
            else index
        )
        seen = {}
        for a in assignments:
            if not a.get("tasks"):
                raise ValueError("Cannot establish exposure history: missing assignment tasks")
            for task in a["tasks"]:
                pid = str(task["input_values"]["photo_id"])
                seen.setdefault(pid, set()).add(a["user_id"])
        rng = random.Random(self.config["seed"] + index)
        controls = list(self.snapshot["bank"]["controls"])
        rng.shuffle(controls)
        suites = []
        for offset in range(0, len(rows), self.config["real_per_page"]):
            group = rows[offset : offset + self.config["real_per_page"]]
            authors = (
                set().union(*(seen.get(str(r["photo_id"]), set()) for r in group))
                if kind == "repeat"
                else set()
            )
            control = next(
                (
                    r
                    for r in controls
                    if workers is None or workers - authors - seen.get(str(r["photo_id"]), set())
                ),
                None,
            )
            if control is None:
                raise ValueError(
                    "No unseen control with an eligible non-author; replenish bank/workers"
                )
            controls.remove(control)
            tasks = [{"input_values": public_image(r)} for r in group + [control]]
            rng.shuffle(tasks)
            suites.append(
                {
                    "tasks": tasks,
                    "overlap": 1,
                    "infinite_overlap": False,
                    "reserved_for": sorted(
                        (workers or set()) - authors - seen.get(str(control["photo_id"]), set())
                    ),
                    "unavailable_for": sorted(authors | seen.get(str(control["photo_id"]), set())),
                }
            )
        batch = {
            "key": f"batch-{index:03}",
            "kind": kind,
            "repeat_reason": repeat_reason,
            "suites": suites,
            "price": self.config["page_price"],
            "overlap": 1,
            "operation_id": str(uuid.uuid4()),
            "uploaded": False,
            "work_ids": [str(r["photo_id"]) for r in rows],
        }
        return batch

    def verify_remote(self, batch):
        def require_fields(expected, actual, location):
            if isinstance(expected, dict):
                if not isinstance(actual, dict):
                    raise ValueError(f"Remote object changed: {location}")
                for key, value in expected.items():
                    require_fields(value, actual.get(key), f"{location}.{key}")
            elif expected != actual:
                raise ValueError(f"Remote object changed: {location}")

        pool = self.api.request("GET", f"/pools/{batch['pool_id']}")
        expected = self.pool_body(batch)
        for field in (
            "project_id",
            "reward_per_assignment",
            "auto_accept_solutions",
            "filter",
            "defaults",
            "assignment_max_duration_seconds",
        ):
            require_fields(expected[field], pool.get(field), field)
        if pool.get("dynamic_pricing_config") or pool.get("dynamic_overlap_config"):
            raise ValueError("Unexpected dynamic price/overlap")
        if "quality_control" in expected:
            require_fields(
                expected["quality_control"], pool.get("quality_control"), "quality_control"
            )
        project = self.api.request("GET", f"/projects/{self.state['resources']['project']}")
        expected_project = project_body(self.snapshot, "bbox:" + self.state["run_id"])
        # The saved project ID establishes identity; its private comment is editable.
        expected_project.pop("private_comment")
        expected_project["public_instructions"] = instruction_structure(
            expected_project["public_instructions"]
        )
        project["public_instructions"] = instruction_structure(project["public_instructions"])
        require_fields(expected_project, project, "project")
        suites = list(self.api.items("/task-suites", pool_id=batch["pool_id"]))
        retired = set(batch.get("retired_suite_ids", []))
        if any(s["overlap"] != 0 or s["infinite_overlap"] for s in suites if s["id"] in retired):
            raise ValueError("Retired staged pages must have zero overlap")
        suites = [s for s in suites if s["id"] not in retired]
        local = {s["remote_id"]: s for s in batch["suites"]}
        if {s["id"] for s in suites} != set(local):
            raise ValueError("Unexpected or missing remote task suites")
        for suite in suites:
            expected_suite = {k: v for k, v in local[suite["id"]].items() if k != "remote_id"}
            expected_tasks = expected_suite.pop("tasks")
            require_fields(expected_suite, suite, "suite")
            if len(suite["tasks"]) != len(expected_tasks):
                raise ValueError("Task suite size changed")
            for task, remote_task in zip(expected_tasks, suite["tasks"], strict=True):
                require_fields(task, remote_task, "task")
                if remote_task.get("known_solutions"):
                    raise ValueError("Private gold unexpectedly attached to a public task")

    def open(self):
        self.require_production()
        batch = self.state["batches"][-1]
        if not batch["uploaded"] or not self.state["readiness"] or not self.state["preflight"]:
            raise ValueError("Need uploaded tasks, preflight and human readiness record")
        report = read_json(self.directory / "preflight.json")
        if digest(report) != self.state["preflight"]["hash"]:
            raise ValueError("Preflight report changed")
        if datetime.now(timezone.utc) - datetime.fromisoformat(report["checked_at"]) > timedelta(
            days=1
        ):
            raise ValueError("Image preflight is older than 24 hours; repeat it")
        check_commitment(self.state["batches"], self.config)
        for other in self.state["batches"][:-1]:
            pool = self.api.request("GET", f"/pools/{other['pool_id']}")
            if pool["status"] == "OPEN":
                raise ValueError("Another pool is open")
        self.verify_remote(batch)
        assignments = self.sync()
        paid = money(0)
        for item in self.state["batches"]:
            by_suite = {}
            for answer in assignments:
                if answer["pool_id"] != item["pool_id"] or answer["status"] not in TERMINAL_ANSWERS:
                    continue
                if money(answer["reward"]) != money(item["price"]):
                    raise ValueError("Actual assignment price differs from the budget")
                by_suite[answer["task_suite_id"]] = by_suite.get(answer["task_suite_id"], 0) + 1
                paid += cost(answer["reward"], 1, self.config["commission"])
            if any(n > item["overlap"] for n in by_suite.values()):
                raise ValueError("Actual assignments exceed finite overlap")
        balance = money(self.api.request("GET", "/requester")["balance"])
        remaining_bbox = max(money(0), money(plan(self.config)["bbox_limit"]) - paid)
        protected = money(self.config["review_reserved"]) + money(self.config["free_reserve"])
        if balance < remaining_bbox + protected:
            raise ValueError("Balance cannot fund remaining bbox and protect review/reserves")
        self.api.wait(self.api.request("POST", f"/pools/{batch['pool_id']}/open", {}))
        batch["opened_at"] = now()
        self.save()

    def export(self, require_complete=False):
        assignments = self.sync()
        self.score_exam(assignments)
        for batch in self.all_batches():
            if batch["kind"] != "exam" and batch.get("uploaded"):
                self.score_controls(assignments, batch)
        progress = self.pool_progress(assignments)
        rows = answers(assignments, self.snapshot)
        batches = {b.get("pool_id"): b for b in self.all_batches()}
        for row in rows:
            row["repeat_reason"] = batches[row["pool_id"]].get("repeat_reason")
            row["worker_quality"] = self.state["workers"].get(row["worker_id"], {})
            row["audit"] = self.state["audits"].get(row["answer_id"])
        for role, filename in (
            ("work", "review.json"),
            ("controls", "controls.json"),
            ("exam", "exam.json"),
        ):
            write_json(self.directory / filename, [r for r in rows if r["role"] == role])
        report = coverage(rows, self.snapshot["manifest"])
        report["pools"] = progress
        accepted_work = {
            row["photo_id"]
            for row in rows
            if row["role"] == "work" and row["assignment_status"] == "ACCEPTED"
        }
        report["collection_complete"] = accepted_work == {
            str(row["photo_id"]) for row in self.snapshot["manifest"]
        } and all(p["complete"] for p in progress if p["kind"] != "exam")
        report["required_audits"] = sorted(
            {
                answer_id
                for batch in self.all_batches()
                for result in batch.get("control_report", {}).values()
                for answer_id in result["required_audits"]
            }
            | {
                row["answer_id"]
                for row in rows
                if row["role"] == "work"
                and row["assignment_status"] == "ACCEPTED"
                and batches[row["pool_id"]]["kind"] == "pilot"
            }
        )
        write_json(self.directory / "coverage.json", report)
        if require_complete and not report["collection_complete"]:
            raise ValueError("Collection incomplete; partial export saved, inspect coverage.json")
        return report

    def status(self):
        assignments = self.sync()
        paid = sum(
            (money(a.get("reward", 0)) for a in assignments if a["status"] == "ACCEPTED"), money(0)
        )
        return {
            "run_id": self.state["run_id"],
            "input_hash": self.state["input_hash"],
            "plan": plan(self.config),
            "committed_with_commission": str(
                committed(self.state["batches"] + self.state.get("staged_batches", []), self.config)
            ),
            "paid_rewards": str(paid),
            "training": self.state.get("resources", {}).get("training"),
            "staged_batches": [
                {"kind": b["kind"], "pool_id": b.get("pool_id"), "uploaded": b["uploaded"]}
                for b in self.state.get("staged_batches", [])
            ],
            "workers": self.state["workers"],
            "pilot": self.state["pilot"],
            "batches": [
                {
                    "key": b["key"],
                    "kind": b["kind"],
                    "pool_id": b.get("pool_id"),
                    "uploaded": b["uploaded"],
                    "released": b.get("released"),
                }
                for b in self.state["batches"]
            ],
        }
