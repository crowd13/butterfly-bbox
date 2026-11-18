import copy
import tempfile
import unittest
from pathlib import Path

from crowd.bbox.api import ApiError
from crowd.review.exam_mode import use_exam, use_native, writable_pool
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json


class ExamApi:
    def __init__(self):
        exam = {
            "project_id": "project", "private_name": "Exam", "will_expire": "2026-12-01T00:00:00",
            "reward_per_assignment": 1, "assignment_max_duration_seconds": 900,
            "auto_accept_solutions": True, "defaults": {"default_overlap_for_new_task_suites": 20},
            "filter": {"and": [{"category": "skill", "key": "exam-skill", "operator": "EQ",
                                 "value": None}]},
            "quality_control": {
                "configs": [{
                    "collector_config": {"type": "GOLDEN_SET", "parameters": {"history_size": 10}},
                    "rules": [{
                        "conditions": [{"key": "correct_answers_rate", "operator": "GTE",
                                        "value": 90}],
                        "action": {"type": "SET_SKILL", "parameters": {
                            "skill_id": "access-skill", "skill_value": 100,
                        }},
                    }],
                }],
                "training_requirement": {"training_pool_id": "training",
                                         "training_passing_skill_value": 0},
            },
        }
        self.state = {
            "resources": {key: key for key in ("project", "training", "exam", "pilot", "main")},
            "definitions": {
                "training": {"project_id": "project", "private_name": "Training"},
                "exam": copy.deepcopy(exam),
                "pilot": {"project_id": "project", "private_name": "Pilot", "filter": {
                    "category": "skill", "key": "exam-skill", "operator": "GTE", "value": 90,
                }},
                "main": {"project_id": "project", "private_name": "Main", "filter": {
                    "category": "skill", "key": "access-skill", "operator": "GTE", "value": 80,
                }},
            },
        }
        self.rows = {
            key: {**copy.deepcopy(value), "id": key, "status": "CLOSED", "created": "date"}
            for key, value in self.state["definitions"].items()
        }
        self.rows["exam"].update(
            metadata={"custom": ["keep"]}, priority=0, owner={"id": "owner"}, type="REGULAR",
            assignments_issuing_config={"issue_task_suites_in_creation_order": False},
        )
        self.rows["exam"]["quality_control"]["configs"][0]["collector_config"]["uuid"] = "keep-uuid"
        self.assignments = {}
        self.calls = []
        self.failure = None

    def items(self, path, **filters):
        self.calls.append(("LIST", path, filters))
        return iter(self.assignments.get(filters["pool_id"], []))

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        key = path.rsplit("/", 1)[-1]
        if method == "GET":
            return copy.deepcopy(self.rows[key])
        if method != "PUT" or path != "/pools/exam":
            raise AssertionError(f"Unexpected mutation: {method} {path}")
        if self.failure == "before":
            raise ApiError("Lost response", None)
        self.rows[key].update(copy.deepcopy(body))
        self.rows[key]["quality_control"] = copy.deepcopy(body["quality_control"])
        if self.failure == "after":
            raise ApiError("Lost response", None)
        if self.failure == "readback":
            self.rows[key]["reward_per_assignment"] = 2
        return copy.deepcopy(self.rows[key])


class ExamModeTests(unittest.TestCase):
    def test_detach_preserves_payment_skills_filters_defaults_and_data(self):
        api = ExamApi()
        before = copy.deepcopy(api.rows)
        with tempfile.TemporaryDirectory() as directory:
            result = use_exam(api, api.state, directory)
            self.assertTrue(result["updated"])
            target = writable_pool(before["exam"])
            target["quality_control"].pop("training_requirement")
            self.assertEqual(writable_pool(api.rows["exam"]), target)
            self.assertEqual(api.state["definitions"]["exam"], target)
            self.assertEqual(api.state["training_mode"], "inline")
            self.assertEqual(read_json(Path(directory) / "state.json"), api.state)
            journal = read_json(Path(directory) / "exam-mode.json")
            self.assertEqual(journal["status"], "applied")
            self.assertEqual(journal["before"], writable_pool(before["exam"]))
            self.assertEqual(journal["differences"], [])
            self.assertFalse(use_exam(api, api.state, directory)["updated"])
        self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)
        put = next(call[2] for call in api.calls if call[0] == "PUT")
        self.assertFalse({"id", "status", "created", "owner", "type"} & put.keys())
        self.assertEqual({key: value for key, value in api.rows.items() if key != "exam"},
                         {key: value for key, value in before.items() if key != "exam"})
        self.assertFalse(any(call[1] in {"/tasks", "/task-suites"} for call in api.calls))

    def test_any_open_resource_or_existing_assignment_blocks_before_put(self):
        for key in ("training", "exam", "pilot", "main"):
            with self.subTest(open=key), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                api.rows[key]["status"] = "OPEN"
                with self.assertRaisesRegex(ValueError, "Close"):
                    use_exam(api, api.state, directory)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))
        for key in ("exam", "pilot", "main"):
            with self.subTest(assignment=key), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                api.assignments[key] = [{"status": "EXPIRED"}]
                with self.assertRaisesRegex(ValueError, "assignments exist"):
                    use_exam(api, api.state, directory)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_remote_conflicts_are_not_adopted(self):
        api = ExamApi()
        api.rows["exam"]["reward_per_assignment"] = 2
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Remote exam settings changed"):
                use_exam(api, api.state, directory)
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_lost_put_response_is_reconciled_before_marking_applied(self):
        api = ExamApi()
        api.failure = "after"
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(use_exam(api, api.state, directory)["updated"])
            self.assertEqual(read_json(Path(directory) / "exam-mode.json")["status"], "applied")
            self.assertFalse(use_exam(api, api.state, directory)["updated"])
        self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)

    def test_unapplied_put_can_resume_without_changing_local_definition(self):
        api = ExamApi()
        before = copy.deepcopy(api.state)
        api.failure = "before"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ApiError):
                use_exam(api, api.state, directory)
            self.assertEqual(api.state, before)
            journal = read_json(Path(directory) / "exam-mode.json")
            self.assertEqual(journal["status"], "unconfirmed")
            self.assertTrue(journal["differences"])
            api.failure = None
            self.assertTrue(use_exam(api, api.state, directory)["updated"])

    def test_changed_readback_blocks_commit_and_later_overwrite(self):
        api = ExamApi()
        before = copy.deepcopy(api.state)
        api.failure = "readback"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                use_exam(api, api.state, directory)
            self.assertEqual(api.state, before)
            api.failure = None
            with self.assertRaisesRegex(ValueError, "outside the prepared migration"):
                use_exam(api, api.state, directory)
        self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)

    def test_pending_journal_from_another_pool_is_rejected(self):
        api = ExamApi()
        with tempfile.TemporaryDirectory() as directory:
            write_json(Path(directory) / "exam-mode.json", {"project_id": "other", "exam": "exam"})
            with self.assertRaisesRegex(ValueError, "another run"):
                use_exam(api, api.state, directory)
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_inline_to_native_restores_only_own_training_and_is_idempotent(self):
        api = ExamApi()
        before = copy.deepcopy(api.rows)
        with tempfile.TemporaryDirectory() as directory:
            use_exam(api, api.state, directory)
            result = use_native(api, api.state, directory)
            self.assertEqual(result["exam"], "exam")
            self.assertEqual(result["training_mode"], "native")
            self.assertTrue(result["updated"])
            self.assertEqual(writable_pool(api.rows["exam"]), writable_pool(before["exam"]))
            self.assertEqual(api.state["definitions"]["exam"], writable_pool(before["exam"]))
            self.assertEqual(api.state["training_mode"], "native")
            self.assertEqual(read_json(Path(directory) / "state.json"), api.state)
            journal = read_json(Path(directory) / "native-mode.json")
            self.assertEqual(journal["status"], "applied")
            self.assertNotIn("training_requirement", journal["before"]["quality_control"])
            self.assertEqual(journal["after"]["quality_control"]["training_requirement"], {
                "training_pool_id": "training", "training_passing_skill_value": 0,
            })
            self.assertEqual(journal["differences"], [])
            self.assertFalse(use_native(api, api.state, directory)["updated"])
        mutations = [call for call in api.calls if call[0] not in {"GET", "LIST"}]
        self.assertEqual([(row[0], row[1]) for row in mutations],
                         [("PUT", "/pools/exam"), ("PUT", "/pools/exam")])
        self.assertEqual({key: value for key, value in api.rows.items() if key != "exam"},
                         {key: value for key, value in before.items() if key != "exam"})

    def test_native_lost_response_reconciles_or_resumes_without_early_commit(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                use_exam(api, api.state, directory)
                before = copy.deepcopy(api.state)
                api.failure = failure
                if failure == "before":
                    with self.assertRaises(ApiError):
                        use_native(api, api.state, directory)
                    self.assertEqual(api.state, before)
                    self.assertEqual(read_json(Path(directory) / "native-mode.json")["status"],
                                     "unconfirmed")
                    api.failure = None
                    self.assertTrue(use_native(api, api.state, directory)["updated"])
                else:
                    self.assertTrue(use_native(api, api.state, directory)["updated"])
                self.assertEqual(api.state["training_mode"], "native")
                self.assertFalse(use_native(api, api.state, directory)["updated"])

    def test_native_keeps_closed_and_no_assignment_guards(self):
        for key in ("training", "exam", "pilot", "main"):
            with self.subTest(open=key), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                use_exam(api, api.state, directory)
                api.calls.clear()
                api.rows[key]["status"] = "OPEN"
                with self.assertRaisesRegex(ValueError, "Close"):
                    use_native(api, api.state, directory)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))
        for key in ("exam", "pilot", "main"):
            with self.subTest(assignment=key), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                use_exam(api, api.state, directory)
                api.calls.clear()
                api.assignments[key] = [{"status": "ACTIVE"}]
                with self.assertRaisesRegex(ValueError, "assignments exist"):
                    use_native(api, api.state, directory)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_native_journal_cannot_change_rules_or_attach_another_training(self):
        for change in ("payment", "training"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                api = ExamApi()
                use_exam(api, api.state, directory)
                use_native(api, api.state, directory)
                path = Path(directory) / "native-mode.json"
                journal = read_json(path)
                if change == "payment":
                    journal["after"]["reward_per_assignment"] = 2
                else:
                    journal["after"]["quality_control"]["training_requirement"][
                        "training_pool_id"
                    ] = "other-training"
                write_json(path, journal)
                api.calls.clear()
                with self.assertRaisesRegex(ValueError, "changes more than"):
                    use_native(api, api.state, directory)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))


if __name__ == "__main__":
    unittest.main()
