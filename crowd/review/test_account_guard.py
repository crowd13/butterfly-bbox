import copy
import tempfile
import unittest
from pathlib import Path

from crowd.review.account_guard import bind_account, verify_bound_account
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json


class AccountApi:
    def __init__(self):
        self.rows = {
            "/requester": {"id": "team", "balance": "472.00"},
            "/projects/11779": {"id": "11779", "owner": {"id": "team", "myself": True}},
            "/projects/11907": {"id": "11907", "owner": {"id": "personal", "myself": False}},
            "/pools/6866512": {"id": "6866512", "project_id": "11779"},
            "/pools/6866513": {"id": "6866513", "project_id": "11779"},
            "/pools/6866514": {"id": "6866514", "project_id": "11779"},
        }
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Account verification must never mutate remote resources")
        return copy.deepcopy(self.rows[path])


class AccountGuardTests(unittest.TestCase):
    def source(self, root):
        path = Path(root) / "source-assignments.json"
        write_json(path, [
            {"id": f"assignment-{pool_id}", "owner": {"id": "team"}, "pool_id": pool_id}
            for pool_id in ("6866512", "6866513", "6866514")
        ])
        return path

    def test_binds_only_after_source_ownership_and_all_pool_checks(self):
        api = AccountApi()
        state = {"resources": {}}
        with tempfile.TemporaryDirectory() as root:
            result = bind_account(api, state, root, "11779", self.source(root))
            self.assertEqual(state["requester_id"], "team")
            self.assertEqual(state["source_project_id"], "11779")
            self.assertEqual(read_json(Path(root) / "state.json"), state)
            self.assertEqual(result["source_pools"], ["6866512", "6866513", "6866514"])
            self.assertEqual(result["balance"], "472.00")
            self.assertEqual(verify_bound_account(api, state)["requester_id"], "team")
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_wrong_account_project_ownership_pool_and_existing_run_stop_without_local_pin(self):
        for change in ("requester", "source_owner", "source_myself", "pool", "old_run"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                api = AccountApi()
                state = {"resources": {}}
                source = self.source(root)
                if change == "requester":
                    api.rows["/requester"]["id"] = "personal"
                elif change == "source_owner":
                    api.rows["/projects/11779"]["owner"]["id"] = "other"
                elif change == "source_myself":
                    api.rows["/projects/11779"]["owner"]["myself"] = False
                elif change == "pool":
                    api.rows["/pools/6866514"]["project_id"] = "11755"
                else:
                    state["resources"]["project"] = "11907"
                original = copy.deepcopy(state)
                with self.assertRaises(ValueError):
                    bind_account(api, state, root, "11779", source)
                self.assertEqual(state, original)
                self.assertFalse((Path(root) / "state.json").exists())
                self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_mixed_or_missing_assignment_owners_are_rejected(self):
        for owner in ({"id": "personal"}, {}):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as root:
                api = AccountApi()
                path = self.source(root)
                rows = read_json(path)
                rows[1]["owner"] = owner
                write_json(path, rows)
                with self.assertRaisesRegex(ValueError, "Source assignment owner"):
                    bind_account(api, {"resources": {}}, root, "11779", path)
                self.assertFalse((Path(root) / "state.json").exists())

    def test_existing_account_and_source_pins_cannot_be_rebound(self):
        for field, value in (("requester_id", "personal"), ("source_project_id", "11755")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                api = AccountApi()
                state = {"resources": {}, field: value}
                before = copy.deepcopy(state)
                with self.assertRaisesRegex(ValueError, "differs"):
                    bind_account(api, state, root, "11779", self.source(root))
                self.assertEqual(state, before)
                self.assertFalse((Path(root) / "state.json").exists())

    def test_unpinned_verification_is_noop_and_bound_mismatch_is_rejected(self):
        api = AccountApi()
        self.assertIsNone(verify_bound_account(api, {"resources": {}}))
        self.assertEqual(api.calls, [])
        with self.assertRaisesRegex(ValueError, "bound requester"):
            verify_bound_account(api, {"requester_id": "personal"})
        self.assertTrue(all(method == "GET" for method, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
