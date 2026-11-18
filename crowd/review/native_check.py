import copy
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from crowd.bbox.api import ApiError
from crowd.review.interface_update import write_json
from crowd.review.prepare import VERDICTS, read_json, training_tasks
from crowd.review.verification import differences, verify_resources, verify_tasks


def build_check_plan(creator):
    body = copy.deepcopy(creator.state["definitions"]["exam"])
    body.update(
        private_name="Проверка завершения обучения — 2 исполнителя, 0 ₽",
        private_comment="review:" + creator.state["run_id"] + ":native-check",
        reward_per_assignment=0,
        defaults={"default_overlap_for_new_task_suites": 2},
    )
    body["quality_control"] = {
        "training_requirement": {
            "training_pool_id": creator.state["resources"]["training"],
            "training_passing_skill_value": 0,
        }
    }
    tasks = training_tasks(creator.plan)
    for task in tasks:
        task.pop("message_on_unknown_solution")
        task.pop("infinite_overlap")
    return {
        "project_id": creator.state["resources"]["project"],
        "training_id": creator.state["resources"]["training"],
        "pool_body": body,
        "task_suites": [{"tasks": tasks, "overlap": 2, "infinite_overlap": False}],
    }


class NativeCheck:
    def __init__(self, creator):
        self.creator = creator
        self.api = creator.api
        self.directory = creator.directory
        self.path = self.directory / "native-check-state.json"
        plan_path = self.directory / "native-check-plan.json"
        self.plan = read_json(plan_path) if plan_path.exists() else build_check_plan(creator)
        if not plan_path.exists():
            write_json(plan_path, self.plan)
        self.state = read_json(self.path) if self.path.exists() else {
            "resources": {}, "definitions": {}, "phase": "prepared",
            "project_id": creator.state["resources"]["project"],
        }
        self.marker = "review:" + creator.state["run_id"] + ":native-check"
        self.training_id = creator.state["resources"]["training"]
        self.paid = [creator.state["resources"][key] for key in ("exam", "pilot", "main")]
        if self.plan["project_id"] != creator.state["resources"]["project"] or (
            self.plan["training_id"] != self.training_id
        ) or self.state["project_id"] != self.plan["project_id"]:
            raise ValueError("Native check plan belongs to another project")
        suites = self.plan["task_suites"]
        if len(suites) != 1 or suites[0]["overlap"] != 2 or suites[0].get("infinite_overlap"):
            raise ValueError("Native check must have one finite page and two assignments")
        if self.plan["pool_body"]["reward_per_assignment"] != 0 or any(
            not task.get("known_solutions") for task in suites[0]["tasks"]
        ):
            raise ValueError("Native check must be free and contain only control tasks")
        expected = training_tasks(creator.plan)
        for task in expected:
            task.pop("message_on_unknown_solution")
            task.pop("infinite_overlap")
        if suites[0]["tasks"] != expected:
            raise ValueError("Native check must use exactly the six public practice examples")
        quality = self.plan["pool_body"]["quality_control"]
        if set(quality) != {"training_requirement"} or quality["training_requirement"] != {
            "training_pool_id": self.training_id, "training_passing_skill_value": 0,
        }:
            raise ValueError("Native check must require this training and never set work skills")
        counts = Counter(row["worker_id"] for row in creator.plan["source"])
        testers = sorted(counts, key=lambda worker: (-counts[worker], worker))[:2]
        self.state.setdefault("testers", testers)
        if len(self.state["testers"]) != 2 or len(set(self.state["testers"])) != 2:
            raise ValueError("Two distinct bbox workers are required for the closed test")
        self.save()

    def save(self):
        write_json(self.path, self.state)

    def guard_paid_pools(self):
        for pool_id in self.paid:
            if self.api.request("GET", f"/pools/{pool_id}")["status"] != "CLOSED":
                raise ValueError(f"Paid pool {pool_id} must remain CLOSED")

    def ensure(self, key, path, body):
        if key in self.state["resources"]:
            return self.state["resources"][key]
        previous = self.state["definitions"].get(key)
        markers = {body["private_comment"]}
        if previous:
            markers.add(previous["private_comment"])
        found = [row for row in self.api.items(path) if row.get("private_comment") in markers]
        if len(found) > 1:
            raise ValueError("Several native check resources have the same marker")
        if found and previous and found[0].get("private_comment") == previous["private_comment"]:
            body = previous
        self.state["definitions"][key] = copy.deepcopy(body)
        self.save()
        row = found[0] if found else self.api.request("POST", path, body)
        self.state["resources"][key] = row["id"]
        self.save()
        return row["id"]

    def stage(self):
        self.guard_paid_pools()
        self.creator.verify()
        gate = self.ensure("gate", "/skills", {
            "name": "Review test " + self.creator.state["run_id"][:8], "hidden": True,
            "private_comment": "review:" + self.creator.state["run_id"] + ":test",
        })
        for worker in self.state["testers"]:
            self.api.skill(gate, worker, 100)
        body = copy.deepcopy(self.plan["pool_body"])
        body["filter"]["and"].append({"or": [{
            "category": "skill", "key": gate, "operator": "EQ", "value": 100,
        }]})
        pool_id = self.ensure("pool", "/pools", body)
        suite = copy.deepcopy(self.plan["task_suites"][0])
        suite["reserved_for"] = self.state["testers"]
        self.state.setdefault("suites", [suite])
        self.save()
        self.creator.upload_items("native-check", pool_id, self.state["suites"], "/task-suites")
        verify_resources(self.api, {
            "resources": {"native-check": pool_id},
            "definitions": {"native-check": self.state["definitions"]["pool"]},
        })
        verify_tasks(self.api, "/task-suites", pool_id, self.state["suites"])
        return pool_id

    def transition(self, path, resource_id, action):
        expected = "OPEN" if action == "open" else "CLOSED"
        url = f"{path}/{resource_id}"
        operations = self.state.setdefault("transition_operations", {})
        previous = operations.get(url + "/open")
        if previous:
            operation = self.api.request("GET", "/operations/" + previous)
            if operation.get("status") in {"PENDING", "RUNNING"}:
                completed = self.api.wait(operation)
                if completed.get("status") != "SUCCESS":
                    raise ValueError(f"Previous open operation is still unresolved: {resource_id}")
        current = self.api.request("GET", url)["status"]
        uncertain = self.state.setdefault("uncertain_transitions", {})
        if action == "close" and uncertain.get(url + "/open") and current == "CLOSED":
            raise ValueError(f"Previous open response is unknown; check {resource_id} in requester UI")
        if current == expected:
            uncertain.pop(url + "/" + action, None)
            self.save()
            return
        try:
            operation = self.api.request("POST", url + "/" + action)
        except ApiError as error:
            if self.api.request("GET", url)["status"] != expected:
                if error.status is None:
                    uncertain[url + "/" + action] = True
                    self.save()
                raise
        else:
            if operation.get("id"):
                operations[url + "/" + action] = operation["id"]
                self.save()
            self.api.wait(operation)
        if self.api.request("GET", url)["status"] != expected:
            raise ValueError(f"Transition {action} is not confirmed for {resource_id}")
        uncertain.pop(url + "/" + action, None)
        if action == "close":
            uncertain.pop(url + "/open", None)
        self.save()

    def ready(self):
        pool_id = self.state["resources"]["pool"]
        rules = self.api.request("GET", f"/api/new/requester/pools/{pool_id}/validate")
        if not isinstance(rules, list):
            raise ValueError("Unexpected pool validation response")
        blockers = [row for row in rules if row.get("active") and row.get("blocker")]
        other = [row["type"] for row in blockers if row["type"] != "MUST_PASS_MODERATION"]
        if other:
            raise ValueError("Native check blockers: " + ", ".join(other))
        if not blockers:
            return True
        if not self.state.get("moderation_submitted"):
            if self.state.get("moderation_pending"):
                raise ValueError("Moderation response was lost; check the pool in the requester UI")
            self.state["moderation_pending"] = True
            self.save()
            try:
                result = self.api.request(
                    "PUT", f"/api/new/requester/poolModeration/{pool_id}", {"status": "READY"}
                )
            except ApiError as error:
                if error.status is not None:
                    self.state["moderation_pending"] = False
                    self.save()
                raise
            if result.get("status") != "READY":
                raise ValueError("Moderation submission is not confirmed")
            self.state["moderation_submitted"] = True
            self.state["moderation_pending"] = False
            self.save()
        return False

    def open(self):
        self.guard_paid_pools()
        if not self.ready():
            self.state["phase"] = "moderation"
            self.save()
            return False
        training = self.api.request("GET", f"/trainings/{self.training_id}")
        if training["status"] == "OPEN" and not self.state.get("owns_training_open"):
            raise ValueError("Training was opened outside this test; stop and reconcile first")
        self.state["owns_training_open"] = True
        self.save()
        self.transition("/trainings", self.training_id, "open")
        self.transition("/pools", self.state["resources"]["pool"], "open")
        self.state["phase"] = "running"
        self.save()
        return True

    def report(self):
        self.guard_paid_pools()
        pool_id = self.state["resources"]["pool"]
        remote = self.api.request("GET", f"/pools/{pool_id}")
        expected_gate = self.state["definitions"]["pool"]["quality_control"]
        if remote["reward_per_assignment"] != 0 or remote["quality_control"].get("configs") or (
            differences(remote["quality_control"], expected_gate)
        ):
            raise ValueError("Native check payment or training requirement changed")
        expected = {
            task["input_values"]["answer_id"]: task
            for task in self.state["suites"][0]["tasks"]
        }
        rows, seen, workers = [], set(), set()
        for assignment in self.api.items("/assignments", pool_id=pool_id):
            if assignment["status"] != "ACCEPTED":
                continue
            worker = assignment["user_id"]
            if assignment["pool_id"] != pool_id or worker not in self.state["testers"]:
                raise ValueError("Unexpected native check worker or pool")
            if assignment["id"] in seen or worker in workers:
                raise ValueError("Native check assignments and workers must be distinct")
            seen.add(assignment["id"])
            workers.add(worker)
            answers = {}
            for task, solution in zip(assignment["tasks"], assignment["solutions"], strict=True):
                answer_id = task["input_values"]["answer_id"]
                if answer_id not in expected or task["input_values"] != expected[answer_id]["input_values"]:
                    raise ValueError("Native check answer content changed")
                if answer_id in answers:
                    raise ValueError("Duplicate native check task")
                verdict = solution["output_values"].get("verdict")
                if not isinstance(verdict, str) or verdict not in VERDICTS:
                    raise ValueError("Invalid native check verdict")
                answers[answer_id] = verdict
            if set(answers) != set(expected):
                raise ValueError("Incomplete native check answers")
            correct = sum(
                answers[key] == task["known_solutions"][0]["output_values"]["verdict"]
                for key, task in expected.items()
            )
            rows.append({"assignment_id": assignment["id"], "worker_id": worker,
                         "correct": correct, "total": len(expected)})
        if len(rows) > 2:
            raise ValueError("Native check exceeded two assignments")
        result = {
            "pool_id": pool_id, "pool_status": remote["status"],
            "phase": self.state["phase"], "maximum_payment": 0,
            "accepted": len(rows), "finished": len(rows) == 2,
            "native_gate_verified": len(rows) == 2,
            "answers_passed": len(rows) == 2 and all(row["correct"] == row["total"] for row in rows),
            "assignments": rows,
        }
        write_json(self.directory / "native-check-report.json", result)
        return result

    def close(self):
        pool_id = self.state["resources"].get("pool")
        resources = [("/pools", pool_id)] if pool_id else []
        if self.state.get("owns_training_open"):
            resources.insert(0, ("/trainings", self.training_id))
        errors = []
        for path, resource_id in resources:
            try:
                self.transition(path, resource_id, "close")
            except (ApiError, ValueError) as error:
                errors.append(str(error))
        if errors:
            self.state.update(phase="cleanup_pending", close_errors=errors)
            self.save()
            raise ValueError("Native check cleanup is not confirmed: " + "; ".join(errors))
        self.state["phase"] = "closed"
        self.state["owns_training_open"] = False
        self.save()

    def stop(self):
        self.state["owns_training_open"] = True
        self.save()
        self.close()
        return self.report()

    def run(self, open_test=False, watch_seconds=0):
        cleanup = open_test or self.state.get("owns_training_open", False)
        try:
            self.stage()
            result = self.report()
            if result["finished"]:
                cleanup = True
            if open_test and not result["finished"]:
                started = datetime.now(timezone.utc)
                write_json(self.directory / "native-check-watch.json", {
                    "pool_id": self.state["resources"]["pool"],
                    "watch_seconds": watch_seconds,
                    "started_at": started.isoformat(),
                    "deadline_at": (started + timedelta(seconds=watch_seconds)).isoformat(),
                })
            deadline = time.monotonic() + watch_seconds
            while open_test and not result["finished"]:
                if self.state["phase"] != "running":
                    self.open()
                result = self.report()
                if self.state["phase"] == "running" and result["pool_status"] == "CLOSED" and not result["finished"]:
                    raise ValueError("Native check pool closed before both answers were accepted")
                print(json.dumps({key: result[key] for key in (
                    "pool_id", "phase", "pool_status", "accepted", "finished", "maximum_payment"
                )}, ensure_ascii=False), flush=True)
                if result["finished"] or not watch_seconds or time.monotonic() >= deadline:
                    break
                time.sleep(min(30, max(0, deadline - time.monotonic())))
        finally:
            if cleanup:
                self.close()
        return self.report() if cleanup else result
