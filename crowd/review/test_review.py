import copy
import tempfile
import unittest
from pathlib import Path

from crowd.bbox.api import ApiError
from crowd.review.export import export
from crowd.review.prepare import read_json
from crowd.review.project import Creator, build
from crowd.review.render import project_body


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
        controls = [suite["tasks"][-1]["input_values"]["photo_id"] for suite in suites]
        self.assertEqual(len(set(controls)), 80)
        golden = [suite["tasks"][-1] for suite in suites]
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

    def test_interface_inputs_are_available(self):
        fields = project_body("test")["task_spec"]["input_spec"]
        self.assertTrue(
            all(not field.get("hidden", False) for field in fields.values())
        )

    def test_creation_keeps_every_pool_closed(self):
        class Api:
            def __init__(self):
                self.calls = []
                self.operations = {}

            def items(self, path, **filters):
                return iter(())

            def request(self, method, path, body=None, query=None):
                self.calls.append((method, path, body, query))
                if method == "GET" and path.startswith("/operations/"):
                    operation = self.operations.get(path.rsplit("/", 1)[-1])
                    if operation is None:
                        raise ApiError("missing", 404)
                    return operation
                if path in {"/tasks", "/task-suites"}:
                    operation = {"id": query["operation_id"], "status": "SUCCESS"}
                    self.operations[operation["id"]] = operation
                    return operation
                return {"id": f"resource-{len(self.calls)}"}

            def wait(self, operation):
                return operation

        api = Api()
        with tempfile.TemporaryDirectory() as directory:
            state = Creator(Path(directory), api).create()
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

    def test_export_requires_two_independent_reviewers(self):
        source = [
            {
                "answer_id": "a",
                "photo_id": "1",
                "worker_id": "author",
                "solution": {"status": "no_butterflies", "boxes": []},
            }
        ]
        task = {"input_values": {"answer_id": "a"}}

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
