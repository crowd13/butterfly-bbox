import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError
from crowd.review.cleanup import PROJECTS, archive_projects
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json


class CleanupApi:
    def __init__(self):
        self.rows = {
            "/projects": [
                {"id": "11899", "public_name": "Проверьте рамки бабочек", "status": "ACTIVE"},
                {"id": "11150", "public_name": "Дорожные фото", "status": "ACTIVE"},
                {"id": "11900", "public_name": "Проверьте рамки бабочек", "status": "ACTIVE"},
                {"id": "11755", "public_name": "Найдите бабочек на фотографиях", "status": "ACTIVE"},
                {"id": "11754", "public_name": "ТЕСТ — Найдите бабочек на фотографиях", "status": "ACTIVE"},
            ],
            "/pools": [{"id": "pool", "project_id": "11899", "private_name": "Экзамен",
                        "status": "CLOSED"}],
            "/trainings": [{"id": "training", "project_id": "11899", "private_name": "Обучение",
                            "status": "OPEN"}],
        }
        self.calls = []
        self.assignments = []
        self.lost = False

    def items(self, path, **filters):
        self.calls.append(("LIST", path))
        if path == "/assignments":
            return iter(self.assignments)
        return iter(copy.deepcopy([
            row for row in self.rows[path] if row["project_id"] == filters["project_id"]
        ]))

    def request(self, method, path):
        self.calls.append((method, path))
        if method == "GET":
            prefix, resource_id = path.rsplit("/", 1)
            return copy.deepcopy(next(row for row in self.rows[prefix] if row["id"] == resource_id))
        prefix, resource_id, action = path.rsplit("/", 2)
        row = next(row for row in self.rows[prefix] if row["id"] == resource_id)
        if prefix == "/projects":
            if any(child["status"] != "ARCHIVED" for key in ("/pools", "/trainings")
                   for child in self.rows[key] if child["project_id"] == resource_id):
                raise AssertionError("Children must be archived first")
        row["status"] = "CLOSED" if action == "close" else "ARCHIVED"
        if self.lost:
            raise ApiError("Lost response")
        return {"status": "SUCCESS"}

    def wait(self, operation):
        return operation

    def account(self):
        return {
            "project_inventory_complete": True, "errors": [],
            "projects": [{
                **self.rows["/projects"][0],
                "pool_inventory_complete": True, "training_inventory_complete": True,
                "pools": copy.deepcopy(self.rows["/pools"]),
                "trainings": [{"settings": copy.deepcopy(row)} for row in self.rows["/trainings"]],
            }],
        }


class CleanupTests(unittest.TestCase):
    def test_scope_is_only_seven_existing_review_projects(self):
        self.assertEqual(set(PROJECTS), {
            "11842", "11843", "11886", "11887", "11890", "11898", "11899"
        })
        self.assertEqual(set(PROJECTS.values()), {"Проверьте рамки бабочек"})

    def run_cleanup(self, api, root):
        account = Path(root) / "account.json"
        if not account.exists():
            write_json(account, api.account())
        with patch("crowd.review.cleanup.ACCOUNT", account), patch(
            "crowd.review.cleanup.PROJECTS", {"11899": "Проверьте рамки бабочек"}
        ):
            return archive_projects(api, Path(root) / "cleanup", "11900")

    def test_archives_children_first_preserves_snapshots_and_other_projects(self):
        api = CleanupApi()
        original = copy.deepcopy(api.rows)
        with tempfile.TemporaryDirectory() as root:
            result = self.run_cleanup(api, root)
            self.assertEqual(result["archived"], ["11899"])
            backup = read_json(Path(root) / "cleanup/project-11899-before.json")
            self.assertEqual(backup["project"], original["/projects"][0])
            self.assertEqual(backup["trainings"][0]["status"], "OPEN")
            self.assertEqual(read_json(Path(root) / "cleanup/account-before.json"), api.account()
                             | {"projects": [{**api.account()["projects"][0],
                                               **original["/projects"][0],
                                               "pools": original["/pools"],
                                               "trainings": [{"settings": original["/trainings"][0]}]}]})
            posts = sum(method == "POST" for method, _ in api.calls)
            self.run_cleanup(api, root)
            self.assertEqual(sum(method == "POST" for method, _ in api.calls), posts)
        self.assertEqual(api.rows["/projects"][1:], original["/projects"][1:])
        self.assertFalse(any(method in {"DELETE", "PUT", "PATCH"} for method, _ in api.calls))
        self.assertFalse(any(path in {"/tasks", "/task-suites"} for _, path in api.calls))

    def test_lost_response_is_reconciled_by_status(self):
        api = CleanupApi()
        api.lost = True
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_cleanup(api, root)["archived"], ["11899"])
        self.assertEqual(api.rows["/projects"][0]["status"], "ARCHIVED")

    def test_renamed_resource_wrong_owner_and_extra_child_block_before_mutation(self):
        for change in ("name", "bbox_name", "owner", "child"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                api = CleanupApi()
                write_json(Path(root) / "account.json", api.account())
                if change == "name":
                    api.rows["/projects"][0]["public_name"] = "Other project"
                elif change == "bbox_name":
                    api.rows["/projects"][0]["public_name"] = "Найдите бабочек на фотографиях"
                elif change == "owner":
                    api.rows["/projects"][0]["owner"] = {"myself": False}
                else:
                    api.rows["/pools"].append({"id": "unknown", "project_id": "11899"})
                with self.assertRaises(ValueError):
                    self.run_cleanup(api, root)
                self.assertFalse(any(method == "POST" for method, _ in api.calls))

    def test_unsettled_assignments_block_archival_and_keep_results(self):
        api = CleanupApi()
        api.assignments = [{"id": "active", "status": "ACTIVE"}]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "Unsettled assignments"):
                self.run_cleanup(api, root)
        self.assertEqual(api.assignments, [{"id": "active", "status": "ACTIVE"}])
        self.assertFalse(any(method == "POST" for method, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
