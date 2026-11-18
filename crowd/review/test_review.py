import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError
from crowd.review.export import export
from crowd.review.prepare import public_input, read_json
from crowd.review.project import Creator, build
from crowd.review.render import project_body
from crowd.review.verification import verify_upload


class Api:
    def __init__(self):
        self.calls = []
        self.operations = {}
        self.rows = {}

    def items(self, path, **filters):
        return iter(copy.deepcopy([
            row for row in self.rows.get(path, [])
            if all(row.get(key) == value for key, value in filters.items())
        ]))

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path, copy.deepcopy(body), query))
        if method == "GET":
            prefix, key = path.rsplit("/", 1)
            if prefix == "/operations":
                if key not in self.operations:
                    raise ApiError("missing", 404)
                return self.operations[key]
            return next(copy.deepcopy(row) for row in self.rows[prefix] if row["id"] == key)
        if path in {"/tasks", "/task-suites"}:
            self.rows.setdefault(path, []).extend(copy.deepcopy(body))
            operation = {
                "id": query["operation_id"], "status": "SUCCESS",
                "details": {"success_count": len(body), "failed_count": 0},
            }
            self.operations[operation["id"]] = operation
            return operation
        resource = {**copy.deepcopy(body), "id": f"resource-{len(self.calls)}", "status": "CLOSED"}
        self.rows.setdefault(path, []).append(resource)
        return resource

    def wait(self, operation):
        return operation

    def skill(self, skill_id, user_id, value):
        self.calls.append(("PUT", "/user-skills", (skill_id, user_id, value), None))


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build()

    def test_complete_plan(self):
        self.assertEqual(len(self.plan["pilot"]), 8)
        self.assertEqual(len(self.plan["main"]), 72)
        self.assertEqual(len(self.plan["examples"]["controls"]), 80)
        self.assertEqual(self.plan["budget"]["total_with_commission"], "196.0000")
        suites = self.plan["pilot"] + self.plan["main"]
        work = [
            task
            for suite in suites
            for task in suite["tasks"]
            if "known_solutions" not in task
        ]
        self.assertEqual(len(work), 400)
        self.assertEqual(len({task["input_values"]["answer_id"] for task in work}), 400)
        self.assertTrue(
            all(len(suite["tasks"]) == 6 and suite["overlap"] == 2 for suite in suites)
        )
        golden = [
            next(task for task in suite["tasks"] if "known_solutions" in task)
            for suite in suites
        ]
        controls = [task["input_values"]["photo_id"] for task in golden]
        self.assertEqual(len(set(controls)), 80)
        self.assertTrue(
            all("message_on_unknown_solution" not in task for task in golden)
        )
        self.assertTrue(
            all(
                "message_on_unknown_solution" not in task
                for suite in self.plan["exam"]
                for task in suite["tasks"]
            )
        )

    def test_bbox_authors_are_excluded(self):
        source = {
            row["answer_id"]: row
            for row in read_json("crowd/bbox/runs/final/review.json")
        }
        for suite in self.plan["pilot"] + self.plan["main"]:
            authors = {
                source[task["input_values"]["answer_id"]]["worker_id"]
                for task in suite["tasks"]
                if "known_solutions" not in task
            }
            self.assertEqual(set(suite["unavailable_for"]), authors)

    def test_unboxed_adult_moths_are_taught_and_examined(self):
        for stage, photo_id in (("practice", "762688955"), ("exam", "762096515")):
            with self.subTest(stage=stage):
                row = next(
                    item for item in self.plan["examples"][stage]
                    if item["photo_id"] == photo_id
                )
                self.assertEqual(row["source_status"], "no_butterflies")
                self.assertEqual(row["source_boxes"], [])
                self.assertEqual(row["verdict"], "MISSING")
                self.assertIn("моль", row["hint"])
        self.assertNotEqual(
            self.plan["examples"]["practice"][-1]["photo_id"],
            self.plan["examples"]["exam"][4]["photo_id"],
        )

    def test_interface_inputs_are_available(self):
        fields = project_body("test")["task_spec"]["input_spec"]
        self.assertTrue(
            all(not field.get("hidden", False) for field in fields.values())
        )

    def test_creation_keeps_every_pool_closed(self):
        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(Path(directory), api)
            state = creator.create()
            self.assertTrue(creator.verify()["verified"])
        self.assertEqual(
            set(state["resources"]),
            {
                "project",
                "training",
                "exam_skill",
                "access_skill",
                "exam",
                "pilot",
                "main",
            },
        )
        uploads = [call for call in api.calls if call[1] in {"/tasks", "/task-suites"}]
        self.assertTrue(uploads)
        self.assertTrue(all(call[3]["open_pool"] == "false" for call in uploads))
        pools = [call[2] for call in api.calls if call[1] == "/pools"]
        self.assertTrue(all("review:" not in pool["private_name"] for pool in pools))
        self.assertTrue(
            all(pool["private_comment"].startswith("review:") for pool in pools)
        )
        self.assertTrue(
            all(
                pool["quality_control"]["configs"][0]["collector_config"]["type"]
                == "GOLDEN_SET"
                for pool in pools
            )
        )
        qualified = pools[1:]
        access_filters = [
            rule
            for pool in qualified
            for group in pool["filter"]["and"]
            for rule in group["or"]
            if rule.get("key") == state["resources"]["access_skill"]
        ]
        self.assertTrue(access_filters)
        self.assertTrue(all(rule["operator"] == "GTE" for rule in access_filters))
        training = next(call[2] for call in api.calls if call[1] == "/trainings")
        self.assertEqual(training["task_suites_required_to_pass"], 6)
        self.assertEqual(training["training_tasks_in_task_suite_count"], 1)
        self.assertNotIn("retry_training_after_days", training)
        training_upload = next(call for call in uploads if call[1] == "/tasks")
        schema = project_body("test")["task_spec"]
        for task in training_upload[2]:
            self.assertEqual(set(task["input_values"]), set(schema["input_spec"]))
            self.assertEqual(set(task["known_solutions"][0]["output_values"]), {"verdict"})
            self.assertTrue(task["message_on_unknown_solution"])
            self.assertTrue(task["infinite_overlap"])
        exam = pools[0]
        self.assertEqual(exam["quality_control"]["training_requirement"], {
            "training_pool_id": state["resources"]["training"],
            "training_passing_skill_value": 0,
        })
        filters = [rule for group in exam["filter"]["and"] for rule in group["or"]]
        self.assertIn(
            {"category": "skill", "key": state["resources"]["exam_skill"],
             "operator": "EQ", "value": None}, filters
        )

    def test_bound_run_rejects_another_accounts_token_before_any_creation(self):
        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.state["requester_id"] = "official-account"
            creator.save()
            with patch.object(api, "request", return_value={"id": "personal-account"}) as request:
                with self.assertRaisesRegex(ValueError, "bound requester"):
                    Creator(directory, api)
                request.assert_called_once_with("GET", "/requester")
            self.assertFalse(any(call[0] == "POST" for call in api.calls))

    def test_exam_reporting_and_recovery_cannot_restore_a_lost_work_access(self):
        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            tasks = creator.plan["exam"][0]["tasks"]
            api.rows["/assignments"] = [{
                "id": "exam-a", "status": "ACCEPTED", "user_id": "r",
                "pool_id": creator.state["resources"]["exam"],
                "tasks": tasks,
                "solutions": [
                    {"output_values": task["known_solutions"][0]["output_values"]}
                    for task in tasks
                ],
            }]
            self.assertEqual(creator.score_exam()["exam-a"]["score"], 100)
            self.assertFalse(any(call[0] == "PUT" for call in api.calls))
            creator.score_exam(apply=True)
            creator.score_exam(apply=True)
            self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 2)
            api.rows["/assignments"].append({
                "id": "work-a", "status": "ACTIVE", "user_id": "r",
                "pool_id": creator.state["resources"]["pilot"],
            })
            with self.assertRaisesRegex(ValueError, "before any paid"):
                creator.score_exam(apply=True)

    def test_resume_uses_snapshot_and_never_duplicates_uploads(self):
        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            original = Creator(directory, api)
            original.create()
            posts = sum(call[0] == "POST" for call in api.calls)
            with patch("crowd.review.project.build", side_effect=AssertionError("live data read")):
                resumed = Creator(directory, api)
                resumed.create()
                self.assertTrue(resumed.verify()["verified"])
            self.assertEqual(original.state["run_id"], resumed.state["run_id"])
            self.assertEqual(read_json(Path(directory) / "source.json"), resumed.plan["source"])
            self.assertEqual(posts, sum(call[0] == "POST" for call in api.calls))
            api.rows["/tasks"][0]["known_solutions"] = []
            with self.assertRaisesRegex(ValueError, "content differs"):
                resumed.verify()

    def test_recovery_after_resource_response_is_lost(self):
        api = Api()
        request = api.request

        def lose_response(method, path, body=None, query=None):
            result = request(method, path, body, query)
            if method == "POST" and path == "/projects":
                raise ApiError("uncertain response")
            return result

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(api, "request", side_effect=lose_response):
                with self.assertRaises(ApiError):
                    Creator(directory, api).create()
            Creator(directory, api).create()
            self.assertEqual(len(api.rows["/projects"]), 1)

    def test_operation_success_with_invalid_rows_is_rejected(self):
        for operation in (
            {"id": "x", "status": "RUNNING"},
            {"id": "x", "status": "SUCCESS", "details": {"not_valid_count": 1}},
            {"id": "x", "status": "SUCCESS", "details": {"success_count": 5}},
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                verify_upload(operation, 6)

    def test_control_report_does_not_overwrite_automatic_skill(self):
        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            controls = creator.plan["examples"]["controls"][:5]
            api.rows["/assignments"] = [{
                "id": f"a-{i}", "pool_id": creator.state["resources"]["pilot"],
                "user_id": "r", "status": "ACCEPTED",
                "tasks": [{"input_values": {"answer_id": row["answer_id"]}}],
                "solutions": [{"output_values": {"verdict": row["verdict"]}}],
            } for i, row in enumerate(controls)]
            self.assertTrue(creator.score_controls()["r"]["passed"])
            self.assertFalse(any(call[0] == "PUT" for call in api.calls))
            api.rows["/assignments"][0]["tasks"] = []
            api.rows["/assignments"][0]["solutions"] = []
            with self.assertRaisesRegex(ValueError, "one control"):
                creator.score_controls()

    def test_export_requires_two_independent_reviewers(self):
        source = [
            {
                "answer_id": "a",
                "photo_id": "1",
                "worker_id": "author",
                "image_url": "https://example.org/1.jpg",
                "width": 640,
                "height": 480,
                "solution": {"status": "no_butterflies", "boxes": []},
            }
        ]
        task = {"input_values": public_input(source[0])}

        def assignment(key, worker):
            return {
                "id": key,
                "status": "ACCEPTED",
                "user_id": worker,
                "tasks": [task],
                "solutions": [{"output_values": {"verdict": "OK"}}],
            }

        rows = export([assignment("x", "r1"), assignment("y", "r2")], source)
        self.assertEqual(rows[0]["decision"], "ACCEPT")
        with self.assertRaisesRegex(ValueError, "two independent"):
            export([assignment("x", "r1")], source)
        bad = copy.deepcopy(assignment("x", "author"))
        with self.assertRaisesRegex(ValueError, "Self-review"):
            export([bad, assignment("y", "r2")], source)


if __name__ == "__main__":
    unittest.main()
