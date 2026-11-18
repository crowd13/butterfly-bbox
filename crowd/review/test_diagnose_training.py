import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError
from crowd.review.diagnose_training import collect, main, requested_project_summaries


class ReadOnlyApi:
    def __init__(self):
        self.denied = set()
        self.calls = []
        self.forbid_inventory = False

    def request(self, method, path):
        self.calls.append((method, path, {}))
        if method != "GET":
            raise AssertionError("Diagnostics must never mutate resources")
        if path in self.denied:
            raise ApiError("Forbidden", 403)
        if path == "/requester":
            return {"id": "requester", "balance": "472.15", "private_extra": "not-collected"}
        return {"id": path.rsplit("/", 1)[-1], "owner": {
            "id": "requester", "myself": True, "email": "private@example.invalid",
        }, "task_spec": {
            "output_spec": {"verdict": {"type": "string"}},
            "view_spec": {"script": "var sentinel=1;"},
        }}

    def items(self, path, **filters):
        self.calls.append(("GET", path, filters))
        if path == "/projects":
            if self.forbid_inventory:
                raise AssertionError("Scoped diagnostics must not enumerate the account")
            return iter([{"id": "reference"}, {"id": "other"}])
        if path == "/trainings":
            return iter([{"id": "training-" + filters["project_id"], "task_suites_required_to_pass": 6}])
        if path == "/pools":
            return iter([{
                "id": "pool", "project_id": filters["project_id"], "status": "CLOSED",
                "reward_per_assignment": "0.75",
                "owner": {"id": "requester", "myself": True, "email": "private@example.invalid"},
                "quality_control": {"training_requirement": {"training_pool_id": "training"}},
            }])
        if path == "/tasks":
            return iter([{"id": "task", "input_values": {"photo_id": "123"},
                          "known_solutions": [{"output_values": {"verdict": "OK"}}],
                          "infinite_overlap": True}])
        raise AssertionError(path)


class DiagnosticTests(unittest.TestCase):
    def test_reads_exact_projects_and_other_account_projects_without_writes(self):
        report = collect(ReadOnlyApi(), "current", "reference")
        self.assertEqual({row["id"] for row in report["projects"]}, {"current", "reference", "other"})
        self.assertEqual(report["errors"], [])
        for row in report["projects"]:
            self.assertTrue(row["trainings"][0]["tasks_complete"])
            self.assertEqual(row["trainings"][0]["tasks"][0]["known_solutions"][0]["output_values"], {"verdict": "OK"})

    def test_denied_reference_is_explicit_and_does_not_replace_it(self):
        api = ReadOnlyApi()
        api.denied.add("/projects/reference")
        report = collect(api, "current", "reference")
        self.assertEqual({row["id"] for row in report["projects"]}, {"current", "other"})
        self.assertEqual(report["errors"][0]["path"], "/projects/reference")
        self.assertEqual(report["errors"][0]["status"], 403)

    def test_scoped_collection_reads_only_explicit_projects_and_requester_balance(self):
        api = ReadOnlyApi()
        api.forbid_inventory = True
        report = collect(api, "official", include_other_projects=False)
        self.assertEqual({row["id"] for row in report["projects"]}, {"official"})
        self.assertEqual(report["reference_project"], "official")
        self.assertEqual(report["requester_balance"], "472.15")
        self.assertIsNone(report["inventory"])
        self.assertFalse(report["project_inventory_complete"])
        self.assertFalse(any(path == "/projects" for _, path, _ in api.calls))
        project_reads = [path for _, path, _ in api.calls if path.startswith("/projects/")]
        self.assertEqual(project_reads, ["/projects/official"])
        self.assertTrue(all(filters.get("project_id") == "official" for _, path, filters in api.calls
                            if path in {"/pools", "/trainings"}))
        self.assertNotIn("private@example.invalid", json.dumps(report))
        self.assertNotIn("not-collected", json.dumps(report))
        summary = requested_project_summaries(report)[0]
        self.assertEqual(summary["owner"], {"id": "requester", "myself": True})
        self.assertTrue(summary["owner_matches_requester"])
        self.assertEqual(summary["output_fields"], ["verdict"])
        self.assertEqual(summary["pools"][0]["reward_per_assignment"], "0.75")
        self.assertTrue(summary["pools"][0]["owner_matches_project"])

    def test_scoped_denied_reference_never_falls_back_to_other_projects(self):
        api = ReadOnlyApi()
        api.forbid_inventory = True
        api.denied.add("/projects/reference")
        report = collect(api, "official", "reference", include_other_projects=False)
        self.assertEqual({row["id"] for row in report["projects"]}, {"official"})
        self.assertEqual(report["errors"], [{
            "path": "/projects/reference", "error": "Forbidden", "status": 403,
        }])
        self.assertFalse(any(path == "/projects" or path == "/projects/other"
                             for _, path, _ in api.calls))

    def test_cli_defaults_reference_to_requested_project_and_prints_only_safe_summary(self):
        api = ReadOnlyApi()
        api.forbid_inventory = True
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "inspection.json"
            console = io.StringIO()
            with patch("crowd.review.diagnose_training.TasksApi", return_value=api), patch(
                "sys.argv", ["diagnose_training", "--project", "11779", "--only-requested",
                             "--output", str(output)]
            ), redirect_stdout(console):
                main()
            report = json.loads(output.read_text())
            self.assertEqual(report["reference_project"], "11779")
            summary = json.loads("\n".join(console.getvalue().splitlines()[1:]))
            self.assertTrue(summary["reference_accessible"])
            self.assertEqual(summary["requester_balance"], "472.15")
            self.assertEqual([row["id"] for row in summary["requested_projects"]], ["11779"])
            self.assertEqual(summary["requested_projects"][0]["pools"][0]["status"], "CLOSED")
            self.assertNotIn("var sentinel=1", console.getvalue())
            self.assertNotIn("task_spec", console.getvalue())


if __name__ == "__main__":
    unittest.main()
