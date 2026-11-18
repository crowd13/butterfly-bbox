import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.review.interface_update import write_json
from crowd.review.native_check import NativeCheck, build_check_plan
from crowd.review.project import Creator
from crowd.review.test_review import Api


class NativeApi(Api):
    def __init__(self):
        super().__init__()
        self.moderation_block = False
        self.accept_on_open = False
        self.testers = []
        self.wrong_answer = False

    def request(self, method, path, body=None, query=None):
        if path.endswith("/validate"):
            return [{"type": "MUST_PASS_MODERATION", "active": self.moderation_block, "blocker": True}]
        if path.startswith("/api/new/requester/poolModeration/"):
            self.calls.append((method, path, copy.deepcopy(body), query))
            return {"status": "READY"}
        if method == "POST" and path.rsplit("/", 1)[-1] in {"open", "close"}:
            self.calls.append((method, path, body, query))
            prefix, resource_id, action = path.rsplit("/", 2)
            resource = next(row for row in self.rows[prefix] if row["id"] == resource_id)
            resource["status"] = "OPEN" if action == "open" else "CLOSED"
            operation = {"id": f"transition-{len(self.calls)}", "type": "TEST", "status": "SUCCESS"}
            self.operations[operation["id"]] = operation
            if prefix == "/pools" and action == "open" and self.accept_on_open:
                suite = next(row for row in self.rows["/task-suites"] if row["pool_id"] == resource_id)
                self.rows["/assignments"] = [self.assignment(resource_id, suite, i) for i in range(2)]
                if self.wrong_answer:
                    self.rows["/assignments"][0]["solutions"][0]["output_values"]["verdict"] = "EXTRA"
            return operation
        return super().request(method, path, body, query)

    def assignment(self, pool_id, suite, index):
        return {
            "id": f"answer-{index}", "pool_id": pool_id, "status": "ACCEPTED",
            "user_id": self.testers[index],
            "tasks": [{"input_values": copy.deepcopy(task["input_values"])} for task in suite["tasks"]],
            "solutions": [{"output_values": copy.deepcopy(task["known_solutions"][0]["output_values"])}
                          for task in suite["tasks"]],
        }


class NativeCheckTests(unittest.TestCase):
    def create(self, directory, api):
        creator = Creator(directory, api)
        creator.create()
        check = NativeCheck(creator)
        api.testers = check.state["testers"]
        return creator, check

    def test_closed_stage_is_reserved_for_two_workers_and_does_not_set_production_skills(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            creator, check = self.create(directory, api)
            original = copy.deepcopy(creator.state["resources"])
            result = check.run()
            self.assertEqual(result["pool_status"], "CLOSED")
            self.assertEqual(result["maximum_payment"], 0)
            self.assertFalse(result["finished"])
            self.assertEqual(creator.state["resources"], original)
            self.assertEqual(check.state["suites"][0]["reserved_for"], check.state["testers"])
            puts = [call[2] for call in api.calls if call[1] == "/user-skills"]
            self.assertTrue(all(row[0] == check.state["resources"]["gate"] for row in puts))
            posts = sum(call[0] == "POST" for call in api.calls)
            check.run()
            self.assertEqual(sum(call[0] == "POST" for call in api.calls), posts)
            self.assertFalse(any(call[1].endswith("/open") for call in api.calls))

    def test_two_real_answers_close_both_free_resources_and_never_open_paid_pools(self):
        api = NativeApi()
        api.accept_on_open = True
        with tempfile.TemporaryDirectory() as directory:
            creator, check = self.create(directory, api)
            result = check.run(open_test=True, watch_seconds=1)
            self.assertTrue(result["native_gate_verified"])
            self.assertTrue(result["answers_passed"])
            self.assertEqual(result["phase"], "closed")
            opened = {call[1] for call in api.calls if call[1].endswith("/open")}
            self.assertEqual(opened, {
                "/trainings/" + creator.state["resources"]["training"] + "/open",
                "/pools/" + check.state["resources"]["pool"] + "/open",
            })
            check.guard_paid_pools()
            self.assertEqual(api.request("GET", "/trainings/" + check.training_id)["status"], "CLOSED")

    def test_auto_accepted_wrong_answers_do_not_pass_the_content_check(self):
        api = NativeApi()
        api.accept_on_open = api.wrong_answer = True
        with tempfile.TemporaryDirectory() as directory:
            _, check = self.create(directory, api)
            result = check.run(open_test=True, watch_seconds=1)
            self.assertTrue(result["finished"])
            self.assertFalse(result["answers_passed"])
            self.assertEqual(result["accepted"], 2)

    def test_moderation_is_submitted_once_and_does_not_pretend_to_be_approval(self):
        api = NativeApi()
        api.moderation_block = True
        with tempfile.TemporaryDirectory() as directory:
            _, check = self.create(directory, api)
            check.stage()
            self.assertFalse(check.open())
            self.assertFalse(check.open())
            self.assertEqual(check.state["phase"], "moderation")
            self.assertEqual(sum("poolModeration" in call[1] for call in api.calls), 1)
            self.assertFalse(any(call[1].endswith("/open") for call in api.calls))
            api.moderation_block = False
            self.assertTrue(check.open())
            check.close()

    def test_nonzero_price_and_open_paid_pool_stop_before_test_creation(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            plan = build_check_plan(creator)
            plan["pool_body"]["reward_per_assignment"] = 1
            write_json(Path(directory) / "native-check-plan.json", plan)
            with self.assertRaisesRegex(ValueError, "must be free"):
                NativeCheck(creator)
            plan["pool_body"]["reward_per_assignment"] = 0
            write_json(Path(directory) / "native-check-plan.json", plan)
            check = NativeCheck(creator)
            api.rows["/pools"][0]["status"] = "OPEN"
            posts = sum(call[0] == "POST" for call in api.calls)
            with self.assertRaisesRegex(ValueError, "must remain CLOSED"):
                check.stage()
            self.assertEqual(sum(call[0] == "POST" for call in api.calls), posts)

    def test_resume_cleans_up_even_if_the_first_report_rejects_an_unknown_worker(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            _, check = self.create(directory, api)
            check.stage()
            check.open()
            suite = check.state["suites"][0]
            assignment = api.assignment(check.state["resources"]["pool"], suite, 0)
            assignment["user_id"] = "unexpected-worker"
            api.rows["/assignments"] = [assignment]
            with self.assertRaisesRegex(ValueError, "Unexpected native check worker"):
                check.run(open_test=True, watch_seconds=1)
            self.assertEqual(check.state["phase"], "closed")
            self.assertEqual(api.request("GET", "/trainings/" + check.training_id)["status"], "CLOSED")
            self.assertEqual(api.request("GET", "/pools/" + check.state["resources"]["pool"])["status"], "CLOSED")

    def test_interrupt_closes_the_test_without_opening_any_paid_pool(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            _, check = self.create(directory, api)
            with patch("crowd.review.native_check.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    check.run(open_test=True, watch_seconds=60)
            self.assertEqual(check.state["phase"], "closed")
            check.guard_paid_pools()

    def test_explicit_stop_checks_both_resources_and_refreshes_a_stale_report(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            creator, check = self.create(directory, api)
            check.stage()
            check.open()
            check.report()
            check.state.update(phase="closed", owns_training_open=False)
            check.save()
            before = copy.deepcopy(creator.state["resources"])
            calls = len(api.calls)
            result = check.stop()
            self.assertEqual(result["pool_status"], "CLOSED")
            self.assertEqual(result["phase"], "closed")
            self.assertEqual(creator.state["resources"], before)
            self.assertEqual(api.request("GET", "/trainings/" + check.training_id)["status"], "CLOSED")
            self.assertFalse(any(call[1] in {"/skills", "/pools", "/user-skills", "/tasks", "/task-suites"}
                                 for call in api.calls[calls:]))

    def test_uncreated_invalid_skill_body_is_replaced_and_old_marker_is_still_recoverable(self):
        api = NativeApi()
        with tempfile.TemporaryDirectory() as directory:
            _, check = self.create(directory, api)
            old = {"name": "old", "hidden": True, "private_comment": "old-marker"}
            check.state["definitions"]["gate"] = old
            check.save()
            desired = {"name": "new", "hidden": True, "private_comment": "new-marker"}
            gate = check.ensure("gate", "/skills", desired)
            self.assertEqual(check.state["definitions"]["gate"], desired)
            self.assertTrue(any(row["id"] == gate for row in api.rows["/skills"]))
        with tempfile.TemporaryDirectory() as directory:
            api = NativeApi()
            _, check = self.create(directory, api)
            check.state["definitions"]["gate"] = old
            api.rows["/skills"].append({**old, "id": "old-created"})
            self.assertEqual(check.ensure("gate", "/skills", desired), "old-created")
            self.assertEqual(check.state["definitions"]["gate"], old)


if __name__ == "__main__":
    unittest.main()
