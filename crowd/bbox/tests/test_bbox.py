"""No paid API calls. Synthetic data are only for exercising failure cases."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError, TasksApi
from crowd.bbox.budget import check_commitment, plan
from crowd.bbox.dataset import (
    SCENARIOS,
    is_difficult,
    read_manifest,
    select_manifest,
    validate_inputs,
)
from crowd.bbox.export import answers
from crowd.bbox.quality import control_windows, evaluate, match_count, validate_solution
from crowd.bbox.render import ROOT, assets, instruction_structure, project_body
from crowd.bbox.staging import stage
from crowd.bbox.storage import digest, read_json, write_json
from crowd.bbox.workflow import Run, now, prepare

BOX = {"left": 0.1, "top": 0.1, "width": 0.3, "height": 0.3}
POSITIVE = {"status": "boxes", "boxes": [BOX]}
EMPTY = {"status": "no_butterflies", "boxes": []}


def inputs():
    config = read_json(ROOT / "config.json")

    def image(i):
        return {
            "photo_id": str(i),
            "image_url": f"https://example.org/{i}.jpg",
            "width": 100,
            "height": 100,
            "license_code": "cc0",
            "class": "butterfly",
            "groups": "ordinary",
        }

    manifest = [image(i) for i in range(1, 401)]

    def gold(i):
        return {
            **image(i),
            "solution": copy.deepcopy(POSITIVE),
            "scenarios": sorted(SCENARIOS),
            "hint": "Synthetic fixture, not for production",
            "team_approval": {
                "approved": True,
                "reviewer": "synthetic team reviewer",
                "reviewed_at": "2026-11-15T10:00:00+03:00",
            },
        }

    bank = {
        "practice": [gold(i) for i in range(401, 411)],
        "exam": [gold(i) for i in range(411, 421)],
        "controls": [gold(i) for i in range(421, 511)],
    }
    return manifest, bank, config


class FakeApi:
    def __init__(self):
        self.objects = {
            "/projects": [],
            "/trainings": [],
            "/tasks": [],
            "/skills": [],
            "/pools": [],
            "/task-suites": [],
            "/assignments": [],
        }
        self.operations = {}
        self.calls = []
        self.skills = {}
        self.lose_resource_response = False
        self.lose_upload_response = False

    def items(self, path, **filters):
        return iter(
            copy.deepcopy(
                [r for r in self.objects[path] if all(r.get(k) == v for k, v in filters.items())]
            )
        )

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/requester":
            return {"balance": "850"}
        if path.startswith("/operations/"):
            key = path.split("/")[-1]
            if key not in self.operations:
                raise ApiError("not found", 404)
            return self.operations[key]
        if path.startswith("/task-suites/") and method == "PATCH":
            row = next(r for r in self.objects["/task-suites"] if r["id"] == path.split("/")[-1])
            row.update(body)
            return copy.deepcopy(row)
        if path == "/tasks" and method == "POST":
            key = query["operation_id"]
            if key not in self.operations:
                for row in body:
                    self.objects[path].append(
                        {**copy.deepcopy(row), "id": "task" + str(len(self.objects[path]))}
                    )
                self.operations[key] = {"id": key, "status": "SUCCESS", "type": "TASK.BATCH_CREATE"}
            return self.operations[key]
        if path == "/task-suites" and method == "POST":
            key = query["operation_id"]
            if key not in self.operations:
                for suite in body:
                    suite = copy.deepcopy(suite)
                    suite["id"] = "suite" + str(len(self.objects[path]))
                    for i, task in enumerate(suite["tasks"]):
                        task["id"] = f"{suite['id']}-{i}"
                    self.objects[path].append(suite)
                self.operations[key] = {
                    "id": key,
                    "status": "SUCCESS",
                    "type": "TASK_SUITE.BATCH_CREATE",
                }
            if self.lose_upload_response:
                self.lose_upload_response = False
                raise ApiError("timeout")
            return self.operations[key]
        if method == "POST" and path in {"/projects", "/skills", "/pools", "/trainings"}:
            if path == "/skills" and body["name"] != body["public_name"]["EN"]:
                raise ApiError("name must match public_name.EN", 400)
            row = {
                **copy.deepcopy(body),
                "id": path[1:] + str(len(self.objects[path])),
                "status": "CLOSED",
            }
            self.objects[path].append(row)
            if self.lose_resource_response:
                self.lose_resource_response = False
                raise ApiError("timeout")
            return row
        if path.startswith("/pools/") or path.startswith("/projects/"):
            parts = path.split("/")
            collection = "/" + parts[1]
            row = next(r for r in self.objects[collection] if r["id"] == parts[2])
            if method == "GET":
                return copy.deepcopy(row)
            row["status"] = "OPEN" if parts[-1] == "open" else "CLOSED"
            return {}
        raise AssertionError((method, path, body, query))

    def wait(self, operation):
        return operation

    def skill(self, skill_id, user_id, value):
        self.skills[skill_id, user_id] = value

    def finish(self, batch, workers=("w1",), wrong_controls=False):
        for i, suite in enumerate(self.objects["/task-suites"]):
            if suite["pool_id"] != batch["pool_id"]:
                continue
            repetitions = len(workers) if batch["kind"] == "exam" else 1
            for j in range(repetitions):
                solutions = []
                for task in suite["tasks"]:
                    pid = int(task["input_values"]["photo_id"])
                    solutions.append(
                        {
                            "output_values": copy.deepcopy(
                                EMPTY if wrong_controls and pid >= 421 else POSITIVE
                            )
                        }
                    )
                self.objects["/assignments"].append(
                    {
                        "id": f"a{len(self.objects['/assignments']):04}",
                        "user_id": workers[j]
                        if batch["kind"] == "exam"
                        else workers[i % len(workers)],
                        "pool_id": batch["pool_id"],
                        "task_suite_id": suite["id"],
                        "status": "ACCEPTED",
                        "tasks": copy.deepcopy(suite["tasks"]),
                        "solutions": solutions,
                        "created": "2026-11-13T00:00:00",
                        "submitted": f"2026-11-13T00:{i:02}:01",
                        "reward": batch["price"],
                    }
                )


class InstructionTests(unittest.TestCase):
    def test_platform_formatting_preserves_instruction(self):
        original = (
            "<ol>\n<li>Draw all butterflies</li>\n</ol>"
            '<a href="https://example.org"><img src="photo.jpg"></a>'
        )
        sanitized = (
            "<ol><li>Draw all butterflies</li></ol>"
            '<a rel="nofollow noopener noreferrer" href="https://example.org">'
            '<img src="photo.jpg" /></a>'
        )
        self.assertEqual(instruction_structure(original), instruction_structure(sanitized))
        for changed in (
            sanitized.replace("Draw all", "Draw one"),
            sanitized.replace("photo.jpg", "other.jpg"),
            sanitized.replace("https://example.org", "https://other.org"),
        ):
            self.assertNotEqual(instruction_structure(original), instruction_structure(changed))


class GeometryTests(unittest.TestCase):
    def test_matching_uses_augmenting_paths(self):
        a = {"left": 0, "top": 0, "width": 0.6, "height": 1}
        b = {"left": 0.4, "top": 0, "width": 0.6, "height": 1}
        whole = {"left": 0, "top": 0, "width": 1, "height": 1}
        self.assertEqual(match_count([whole, a], [a, b], 0.5), 2)
        self.assertEqual(match_count([a, a], [a], 0.5), 1)

    def test_status_and_geometry(self):
        self.assertTrue(evaluate(EMPTY, EMPTY)["correct"])
        self.assertFalse(evaluate(EMPTY, POSITIVE)["correct"])
        self.assertIsNone(evaluate({"status": "load_error", "boxes": []}, EMPTY)["correct"])
        for value in [float("nan"), float("inf"), True, -1]:
            with self.assertRaises(ValueError):
                validate_solution({"status": "boxes", "boxes": [{**BOX, "left": value}]})

    def test_failed_early_window_is_not_hidden(self):
        events = [{"id": str(i), "submitted": f"{i:02}", "correct": i >= 2} for i in range(20)]
        self.assertTrue(control_windows(events)["failed"])
        self.assertEqual(control_windows(events)["tail"], [True] * 4)
        self.assertTrue(control_windows(events[:3])["needs_audit"])


class InputTests(unittest.TestCase):
    def test_editor_inputs_survive_platform_hidden_field_filter(self):
        schema = project_body(
            {"assets": {"script": "", "markup": "", "styles": "", "instructions": ""}}, "test"
        )["task_spec"]["input_spec"]
        sample = {
            "photo_id": "1",
            "image_url": "https://example.org/1.jpg",
            "width": 100,
            "height": 200,
        }
        visible = {k: v for k, v in sample.items() if not schema[k].get("hidden", False)}
        self.assertEqual(visible, sample)

    def test_default_budget(self):
        _, _, config = inputs()
        self.assertEqual(plan(config)["bbox_limit"], "430.50")
        self.assertEqual(plan(config)["pilot_pages"], 10)
        self.assertEqual(plan(config)["remaining_pages"], 90)
        config["page_price"] = "3.00"
        with self.assertRaises(ValueError):
            plan(config)

    def test_whole_pages_not_images(self):
        _, _, config = inputs()
        batches = [
            {"price": "2.50", "overlap": 1, "suites": [{}] * 116},
            {"price": "1", "overlap": 20, "suites": [{}]},
        ]
        with self.assertRaises(ValueError):
            check_commitment(batches, config)

    def test_gold_integrity(self):
        rows, bank, config = inputs()
        validate_inputs(rows, bank, config)
        bank["exam"][0]["photo_id"] = rows[0]["photo_id"]
        with self.assertRaises(ValueError):
            validate_inputs(rows, bank, config)

    def test_reviewers_and_capacity(self):
        rows, bank, config = inputs()
        bank["controls"] = bank["controls"][:89]
        with self.assertRaises(ValueError):
            validate_inputs(rows, bank, config)
        rows, bank, config = inputs()
        bank["exam"][0]["team_approval"]["reviewer"] = ""
        with self.assertRaises(ValueError):
            validate_inputs(rows, bank, config)

    def test_team_approval_requires_confirmation_author_and_timestamp(self):
        for change in (
            {"approved": False},
            {"reviewer": " "},
            {"reviewed_at": "not-a-date"},
            {"reviewed_at": "2026-11-15T12:00:00"},
        ):
            with self.subTest(change=change):
                rows, bank, config = inputs()
                bank["practice"][0]["team_approval"].update(change)
                with self.assertRaisesRegex(ValueError, "Team approval"):
                    validate_inputs(rows, bank, config)

    def test_no_private_gold_in_project(self):
        rows, bank, config = inputs()
        snapshot = {"assets": __import__("crowd.bbox.render", fromlist=["assets"]).assets(bank)}
        body = json.dumps(project_body(snapshot, "marker"))
        self.assertIn(bank["practice"][0]["image_url"], body)
        self.assertNotIn(bank["exam"][0]["image_url"], body)
        self.assertNotIn(bank["controls"][0]["image_url"], body)
        instructions = snapshot["assets"]["instructions"]
        self.assertIn("включая мотыльков и молей", instructions)
        self.assertNotIn("Не выделяйте моль", instructions)

    def test_instruction_images_use_only_current_training_answers(self):
        bank = read_json(ROOT / "fixtures/gold/gold.json")
        instructions = assets(bank)["instructions"]
        self.assertEqual(instructions.count("<img "), 6)
        self.assertNotIn("<svg", instructions)
        published = read_json(ROOT / "instruction-examples/manifest.json")
        practice_ids = {r["photo_id"] for r in bank["practice"]}
        for example in published["examples"]:
            self.assertIn(example["photo_id"], practice_ids)
            self.assertIn(published["base_url"] + example["filename"], instructions)
        bank["practice"][0]["solution"]["boxes"][0]["width"] *= 0.9
        with self.assertRaisesRegex(ValueError, "Instruction image is stale"):
            assets(bank)

    def test_selection_deterministic(self):
        rows = []
        for label, count, group in [
            ("butterfly", 368, "ordinary"),
            ("butterfly", 61, "small"),
            ("butterfly", 33, "moth"),
            ("lookalike", 73, "other_insect"),
            ("unrelated", 58, "plant"),
        ]:
            rows.extend(
                {"photo_id": str(len(rows) + i), "class": label, "groups": group}
                for i in range(count)
            )
        chosen = select_manifest(rows)
        self.assertEqual(chosen, select_manifest(rows))
        self.assertEqual(len(chosen), 400)
        self.assertEqual(sum(r["groups"] == "small" for r in chosen), 61)
        self.assertEqual(sum(r["class"] == "butterfly" for r in chosen), 303)
        self.assertEqual(sum(r["class"] == "lookalike" for r in chosen), 57)
        self.assertTrue(any(r["groups"] == "moth" for r in chosen[:40]))
        self.assertFalse(is_difficult({"class": "butterfly", "groups": "moth"}))
        self.assertTrue(is_difficult({"class": "butterfly", "groups": "moth;small"}))

    def test_current_source_and_frozen_dataset(self):
        source = read_manifest(ROOT.parents[1] / "data/manifest.csv")
        chosen = select_manifest(source)
        self.assertEqual(len(chosen), 400)
        self.assertEqual(sum(is_difficult(r) for r in chosen), 61)
        bank = read_json(ROOT / "fixtures/gold/gold.json")
        work = read_manifest(ROOT / "fixtures/gold/working_manifest.csv")
        validate_inputs(work, bank, read_json(ROOT / "config.json"))
        index = {r["photo_id"]: r for r in source}
        for row in work:
            self.assertEqual(row, index[row["photo_id"]])
        all_rows = work + sum(bank.values(), [])
        for key in ("photo_id", "image_url", "observation_id"):
            self.assertEqual(len({r[key] for r in all_rows}), 510)
        for rows in bank.values():
            self.assertTrue(SCENARIOS <= {s for r in rows for s in r["scenarios"]})
            for row in rows:
                validate_solution(row["solution"])
                if "moth" in row["scenarios"]:
                    self.assertEqual(row["class"], "butterfly")
                    self.assertEqual(row["solution"]["status"], "boxes")

    def test_old_moth_rules_rejected(self):
        with self.assertRaisesRegex(ValueError, "Adult moths must be targets"):
            select_manifest([{"photo_id": "1", "class": "lookalike", "groups": "moth"}])
        rows, bank, config = inputs()
        bank["practice"][0]["solution"] = copy.deepcopy(EMPTY)
        with self.assertRaisesRegex(ValueError, "Adult moth gold requires bounding boxes"):
            validate_inputs(rows, bank, config)
        bank["practice"][0].update(
            {"class": "lookalike", "source_groups": "moth", "scenarios": ["empty"]}
        )
        with self.assertRaisesRegex(ValueError, "Adult moths must be targets"):
            validate_inputs(rows, bank, config)


class WorkflowTests(unittest.TestCase):
    def test_test_only_run_creates_closed_resources_but_cannot_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, bank, config = inputs()
            for rows in bank.values():
                for row in rows:
                    row["team_approval"] = None
            with self.assertRaisesRegex(ValueError, "Team approval"):
                prepare(directory, manifest, bank, config)
            prepare(directory, manifest, bank, config, test_only=True)
            api = FakeApi()
            run = Run(directory, api)
            run.create()
            self.assertEqual(len(run.state["batches"]), 1)
            self.assertTrue(run.state["batches"][0]["uploaded"])
            self.assertTrue(api.objects["/projects"][0]["public_name"].startswith("ТЕСТ"))
            # Recover a created skill after losing the local resource reference.
            exam_id = run.state["resources"].pop("exam")
            run.create()
            self.assertEqual(run.state["resources"]["exam"], exam_id)
            self.assertEqual(len(api.objects["/skills"]), 2)
            for action in (run.open, run.require_production, lambda: run.ready({})):
                with self.assertRaisesRegex(ValueError, "Test-only"):
                    action()
            self.assertIsNone(run.snapshot["bank"]["exam"][0]["team_approval"])

    def test_native_training_and_staged_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, bank, config = inputs()
            prepare(directory, manifest, bank, config, with_training=True)
            api = FakeApi()
            run = Run(directory, api)
            stage(run)
            stage(run)
            self.assertEqual(len(api.objects["/trainings"]), 1)
            self.assertEqual(len(api.objects["/tasks"]), 10)
            self.assertEqual(len(api.objects["/pools"]), 3)
            self.assertEqual(len(api.objects["/task-suites"]), 101)
            self.assertEqual([len(b["work_ids"]) for b in run.state["staged_batches"]], [40, 360])
            self.assertEqual(run.state["batches"][-1]["kind"], "exam")
            self.assertEqual(
                api.objects["/pools"][0]["quality_control"]["training_requirement"][
                    "training_pool_id"
                ],
                run.state["resources"]["training"],
            )
            with self.assertRaisesRegex(ValueError, "No qualified"):
                run.next_batch()
            api.finish(run.state["batches"][0], ("w1", "w2", "w3", "w4"))
            run.score_exam()
            pilot_id = run.state["staged_batches"][0]["pool_id"]
            api.lose_upload_response = True
            with self.assertRaises(ApiError):
                run.next_batch()
            run.next_batch()
            self.assertEqual(run.state["batches"][-1]["pool_id"], pilot_id)
            self.assertEqual(len(api.objects["/pools"]), 3)
            run.verify_remote(run.state["batches"][-1])
            self.assertEqual(len(run.state["staged_batches"]), 1)
            self.assertTrue(all(s["reserved_for"] for s in run.state["batches"][-1]["suites"]))
            self.assertFalse(any(c[1].endswith("/open") for c in api.calls))

    def test_draft_gold_cannot_pass_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, bank, config = inputs()
            for rows in bank.values():
                for row in rows:
                    row["team_approval"] = None
            prepare(directory, manifest, bank, config, draft_gold=True)
            with self.assertRaisesRegex(ValueError, "Team approval"):
                Run(directory).ready({})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.manifest, self.bank, self.config = inputs()
        prepare(self.path, self.manifest, self.bank, self.config)
        self.api = FakeApi()
        self.run = Run(self.path, self.api)

    def qualify(self):
        self.run.create()
        self.api.finish(self.run.state["batches"][0], ("w1", "w2", "w3", "w4"))
        self.run.score_exam()

    def audit_batch(self):
        rows = answers(self.run.sync(), self.run.snapshot)
        pool = self.run.state["batches"][-1]["pool_id"]
        self.run.import_audit(
            [
                {
                    "answer_id": r["answer_id"],
                    "reviewer": "human",
                    "reason": "checked",
                    "solution": POSITIVE,
                }
                for r in rows
                if r["pool_id"] == pool and r["role"] == "work"
            ]
        )

    def test_create_recovers_lost_resource_response(self):
        self.api.lose_resource_response = True
        with self.assertRaises(ApiError):
            self.run.create()
        Run(self.path, self.api).create()
        self.assertEqual(len(self.api.objects["/projects"]), 1)

    def test_create_recovers_upload_without_duplicates(self):
        self.api.lose_upload_response = True
        with self.assertRaises(ApiError):
            self.run.create()
        Run(self.path, self.api).create()
        self.assertEqual(len(self.api.objects["/task-suites"]), 1)

    def test_ambiguous_creation_not_reposted(self):
        self.run.state["pending"]["project"] = "unknown"
        self.run.save()
        with self.assertRaises(ValueError):
            self.run.create()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_paid_open_requires_human_and_online_checks(self):
        self.run.create()
        with self.assertRaises(ValueError):
            self.run.open()
        self.assertFalse(any(c[1].endswith("/open") for c in self.api.calls))

    def test_open_checks_remote_overlap_and_preserves_reserves(self):
        self.run.create()
        report = {"checked_at": now(), "input_hash": self.run.state["input_hash"]}
        write_json(self.path / "preflight.json", report)
        self.run.state["preflight"] = {"hash": digest(report)}
        self.run.ready(
            {
                "input_hash": self.run.state["input_hash"],
                "interface_checked": True,
                "visual_duplicates_checked": True,
                "gold_reviewed": True,
                "commission": "0.40",
                "reviewer": "test",
                "reason": "synthetic test only",
            }
        )
        suite = self.api.objects["/task-suites"][0]
        suite["overlap"] = 21
        with self.assertRaisesRegex(ValueError, "suite.overlap"):
            self.run.open()
        suite["overlap"] = 20
        original = self.api.request

        def low_balance(method, path, body=None, query=None):
            return (
                {"balance": "849"} if path == "/requester" else original(method, path, body, query)
            )

        with patch.object(self.api, "request", side_effect=low_balance):
            with self.assertRaisesRegex(ValueError, "Balance"):
                self.run.open()
        self.assertFalse(any(c[1].endswith("/open") for c in self.api.calls))
        self.run.open()
        self.assertEqual(self.api.objects["/pools"][0]["status"], "OPEN")

    def test_full_pilot_and_next_batch(self):
        self.qualify()
        self.run.next_batch()
        pilot = self.run.state["batches"][-1]
        self.assertEqual(len(pilot["suites"]), 10)
        self.assertTrue(all(len(s["tasks"]) == 5 for s in pilot["suites"]))
        self.api.finish(pilot, ("w1",))
        self.run.score_controls()
        with self.assertRaises(ValueError):
            self.run.release("human", "checked")
        self.audit_batch()
        self.run.score_controls()
        self.run.release(
            "human", "Interface and instruction OK; remaining budget and deadline checked"
        )
        self.assertTrue(self.run.state["pilot"]["passed"])
        self.run.next_batch()
        main = self.run.state["batches"][-1]
        self.assertEqual(main["kind"], "main")
        self.assertEqual(len(main["work_ids"]), 360)
        self.assertEqual(len(main["suites"]), 90)
        self.assertFalse(set(main["work_ids"]) & set(pilot["work_ids"]))
        self.assertEqual(len(set(main["work_ids"]) | set(pilot["work_ids"])), 400)
        seen = {
            t["input_values"]["photo_id"]
            for s in pilot["suites"]
            for t in s["tasks"]
            if int(t["input_values"]["photo_id"]) >= 421
        }
        for suite in self.run.state["batches"][-1]["suites"]:
            control = next(t for t in suite["tasks"] if int(t["input_values"]["photo_id"]) >= 421)
            if control["input_values"]["photo_id"] in seen:
                self.assertIn("w1", suite["unavailable_for"])
        controls = [
            t["input_values"]["photo_id"]
            for suite in main["suites"]
            for t in suite["tasks"]
            if int(t["input_values"]["photo_id"]) >= 421
        ]
        self.assertEqual(len(set(controls)), 90)
        self.api.finish(main, ("w2",))
        self.run.score_controls()
        self.run.release("human", "main controls passed")
        self.assertEqual(self.run.export()["answered"], 400)
        with self.assertRaisesRegex(ValueError, "Both work pools already exist"):
            self.run.next_batch()
        self.assertEqual(len(self.run.state["batches"]), 3)
        self.run.next_batch(pilot["work_ids"][:4], "Independent repeat")
        repeated = self.run.state["batches"][-1]
        self.assertEqual(repeated["kind"], "repeat")
        self.assertIn("w1", repeated["suites"][0]["unavailable_for"])

    def test_failed_pilot_cannot_create_main_or_another_pilot(self):
        self.qualify()
        self.run.next_batch()
        pilot = self.run.state["batches"][-1]
        self.api.finish(pilot)
        for assignment in self.api.objects["/assignments"]:
            if assignment["pool_id"] == pilot["pool_id"]:
                for task, solution in zip(
                    assignment["tasks"], assignment["solutions"], strict=True
                ):
                    if int(task["input_values"]["photo_id"]) <= 400:
                        solution["output_values"] = copy.deepcopy(EMPTY)
        self.audit_batch()
        self.run.score_controls()
        self.run.release("human", "Pilot failed, investigate")
        self.assertFalse(self.run.state["pilot"]["passed"])
        with self.assertRaisesRegex(ValueError, "Pilot failed"):
            self.run.next_batch()
        self.assertEqual(len(self.run.state["batches"]), 2)

    def test_controls_latch_and_replay_is_idempotent(self):
        self.qualify()
        self.run.next_batch()
        self.api.finish(self.run.state["batches"][-1], wrong_controls=True)
        self.api.objects["/pools"][-1]["status"] = "OPEN"
        self.api.objects["/assignments"][-1]["status"] = "ACTIVE"
        report = self.run.score_controls()
        self.assertTrue(report["w1"]["failed"])
        count = len(self.run.state["events"])
        self.run.score_controls()
        self.assertEqual(len(self.run.state["events"]), count)
        self.assertEqual(self.run.state["workers"]["w1"]["access"], 0)
        self.run.score_exam()
        self.assertEqual(self.run.state["workers"]["w1"]["access"], 0)

    def test_export_keeps_missing_errors_and_controls_separate(self):
        self.qualify()
        self.run.next_batch()
        pilot = self.run.state["batches"][-1]
        self.api.finish(pilot)
        result = self.run.export()
        self.assertEqual(result["answered"], 40)
        self.assertEqual(len(result["missing"]), 360)
        self.assertEqual(len(read_json(self.path / "review.json")), 40)
        self.assertEqual(len(read_json(self.path / "controls.json")), 10)

    def test_missing_solutions_in_unfinished_assignments(self):
        self.qualify()
        assignment = copy.deepcopy(self.api.objects["/assignments"][0])
        for status in ("ACTIVE", "SKIPPED", "EXPIRED"):
            for solutions in (None, [None], []):
                assignment.update(status=status, solutions=solutions)
                rows = answers([assignment], self.run.snapshot)
                self.assertEqual(len(rows), 10)
                self.assertIsNone(rows[0]["solution"])
                self.assertTrue(rows[0]["validation_error"])

    def test_control_load_error_requires_explicit_decision(self):
        self.qualify()
        self.run.next_batch()
        batch = self.run.state["batches"][-1]
        self.api.finish(batch)
        assignment = self.api.objects["/assignments"][-1]
        index = next(
            i
            for i, t in enumerate(assignment["tasks"])
            if int(t["input_values"]["photo_id"]) >= 421
        )
        assignment["solutions"][index]["output_values"] = {"status": "load_error", "boxes": []}
        self.audit_batch()
        self.run.score_controls()
        with self.assertRaisesRegex(ValueError, "Investigate control"):
            self.run.release("human", "checked work")
        self.run.import_audit(
            [
                {
                    "answer_id": f"{assignment['id']}:{index}",
                    "correct": None,
                    "reviewer": "human",
                    "reason": "Temporary outage confirmed and resolved",
                }
            ]
        )
        self.run.score_controls()
        self.run.release("human", "checked work and resolved outage")

    def test_restore_preserves_each_decision(self):
        self.qualify()
        self.run.next_batch()
        self.api.finish(self.run.state["batches"][-1])
        self.audit_batch()
        self.run.score_controls()
        self.run.release("human", "checked")
        self.run.restore("w1", "first", "first decision")
        self.run.restore("w1", "second", "second decision")
        self.assertEqual(
            [r["reviewer"] for r in self.run.state["restore_history"]], ["first", "second"]
        )
        self.assertEqual(self.run.state["resets"]["w1"]["reviewer"], "second")

    def test_active_assignment_prevents_new_batch(self):
        self.qualify()
        self.run.next_batch()
        self.api.finish(self.run.state["batches"][-1])
        self.api.objects["/assignments"][-1]["status"] = "ACTIVE"
        self.run.score_controls()
        with self.assertRaises(ValueError):
            self.run.release("human", "cannot release active work")
        with self.assertRaises(ValueError):
            self.run.next_batch()

    def test_frozen_snapshot(self):
        content = read_json(self.path / "inputs.json")
        content["config"]["page_price"] = "0.50"
        (self.path / "inputs.json").write_text(json.dumps(content))
        with self.assertRaises(ValueError):
            Run(self.path, self.api)


class PaginationTests(unittest.TestCase):
    def test_cursor_and_all_statuses(self):
        api = TasksApi("fake-not-a-secret")
        with patch.object(
            api,
            "request",
            side_effect=[
                {"items": [{"id": "a"}], "has_more": True},
                {"items": [{"id": "b"}], "has_more": False},
            ],
        ) as call:
            self.assertEqual(
                list(api.items("/assignments", pool_id="p")), [{"id": "a"}, {"id": "b"}]
            )
        self.assertEqual(call.call_args_list[1].kwargs["query"]["id_gt"], "a")
        self.assertNotIn("status", call.call_args_list[1].kwargs["query"])


if __name__ == "__main__":
    unittest.main()
