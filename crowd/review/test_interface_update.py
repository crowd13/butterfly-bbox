import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError
from crowd.review.interface_update import PROJECT_FIELDS, update_interface, write_json
from crowd.review.prepare import read_json
from crowd.review.project import Creator
from crowd.review.test_review import Api


class EditableApi(Api):
    def request(self, method, path, body=None, query=None):
        if method == "PUT" and path.startswith("/projects/"):
            self.calls.append((method, path, copy.deepcopy(body), query))
            project = next(
                row for row in self.rows["/projects"] if row["id"] == path.split("/")[-1]
            )
            automatic = {key: value for key, value in project.items() if key not in PROJECT_FIELDS}
            project.clear()
            project.update(automatic)
            project.update(copy.deepcopy(body))
            return copy.deepcopy(project)
        return super().request(method, path, body, query)


class InterfaceUpdateTests(unittest.TestCase):
    def test_native_mode_removes_the_embedded_practice_from_the_existing_project(self):
        api = EditableApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            resources = copy.deepcopy(creator.state["resources"])
            for mode, count in (("inline", 6), ("native", 0)):
                creator.state["training_mode"] = mode
                creator.update_interface()
                script = api.rows["/projects"][0]["task_spec"]["view_spec"]["script"]
                practice = json.loads(script.splitlines()[0].split(" = ", 1)[1][:-1])
                self.assertEqual(len(practice), count)
            self.assertEqual(creator.state["resources"], resources)

    def test_updates_existing_project_and_snapshot_without_recreating_any_resources(self):
        api = EditableApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            api.rows["/projects"][0]["task_spec"]["view_spec"]["assets"] = {
                "script_urls": ["https://example.org/first.js", "https://example.org/second.js"]
            }
            api.rows["/projects"][0]["localization_config"] = {"default_language": "RU"}
            before = copy.deepcopy(creator.state["resources"])
            uploads = copy.deepcopy(api.rows["/tasks"] + api.rows["/task-suites"])
            view = copy.deepcopy(creator.project["task_spec"]["view_spec"])
            view["script"] += "\nvar feedbackRevision = 1;"
            result = update_interface(api, creator.state, directory, view)
            self.assertTrue(result["updated"])
            self.assertEqual(creator.state["resources"], before)
            self.assertEqual(api.rows["/tasks"] + api.rows["/task-suites"], uploads)
            frozen = read_json(Path(directory) / "snapshot.json")["project"]
            self.assertEqual(frozen["task_spec"]["view_spec"]["script"], view["script"])
            self.assertEqual(
                frozen["task_spec"]["view_spec"]["assets"]["script_urls"],
                ["https://example.org/first.js", "https://example.org/second.js"],
            )
            self.assertEqual(frozen["localization_config"], {"default_language": "RU"})
            self.assertTrue(creator.verify()["verified"])
            self.assertFalse(update_interface(api, creator.state, directory, view)["updated"])
            self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)

    def test_lost_update_response_is_reconciled_by_reading_the_project(self):
        api = EditableApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            view = copy.deepcopy(creator.project["task_spec"]["view_spec"])
            view["script"] += "\nvar feedbackRevision = 2;"
            request = api.request

            def lose_response(method, path, body=None, query=None):
                result = request(method, path, body, query)
                if method == "PUT":
                    raise ApiError("uncertain response")
                return result

            with patch.object(api, "request", side_effect=lose_response):
                self.assertTrue(update_interface(api, creator.state, directory, view)["updated"])
            self.assertTrue(creator.verify()["verified"])
            self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)

    def test_open_pools_and_unexpected_manual_edits_stop_before_mutation(self):
        for conflict in ("pool", "project"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                api = EditableApi()
                creator = Creator(directory, api)
                creator.create()
                original = copy.deepcopy(creator.state)
                view = copy.deepcopy(creator.project["task_spec"]["view_spec"])
                view["script"] += "\nvar feedbackRevision = 3;"
                if conflict == "pool":
                    api.rows["/pools"][0]["status"] = "OPEN"
                else:
                    api.rows["/projects"][0]["public_name"] = "Manually changed"
                with self.assertRaises(ValueError):
                    update_interface(api, creator.state, directory, view)
                self.assertEqual(creator.state, original)
                self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_resume_finishes_local_snapshot_commit_without_repeating_the_put(self):
        api = EditableApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            view = copy.deepcopy(creator.project["task_spec"]["view_spec"])
            view["script"] += "\nvar feedbackRevision = 4;"

            def fail_snapshot(path, value):
                if Path(path).name == "snapshot.json":
                    raise OSError("interrupted snapshot commit")
                write_json(path, value)

            with patch("crowd.review.interface_update.write_json", side_effect=fail_snapshot):
                with self.assertRaises(OSError):
                    update_interface(api, creator.state, directory, view)
            resumed = Creator(directory, api)
            result = update_interface(api, resumed.state, directory, view)
            self.assertFalse(result["updated"])
            self.assertEqual(sum(call[0] == "PUT" for call in api.calls), 1)
            self.assertEqual(
                read_json(Path(directory) / "snapshot.json")["project"],
                resumed.state["definitions"]["project"],
            )

    def test_pending_update_cannot_overwrite_new_remote_settings(self):
        api = EditableApi()
        with tempfile.TemporaryDirectory() as directory:
            creator = Creator(directory, api)
            creator.create()
            view = copy.deepcopy(creator.project["task_spec"]["view_spec"])
            view["script"] += "\nvar feedbackRevision = 5;"
            request = api.request

            def fail_before_put(method, path, body=None, query=None):
                if method == "PUT":
                    raise ApiError("connection failed before update")
                return request(method, path, body, query)

            with patch.object(api, "request", side_effect=fail_before_put):
                with self.assertRaises(ApiError):
                    update_interface(api, creator.state, directory, view)
            api.rows["/projects"][0]["metadata"] = {"manual_edit": "preserve"}
            with self.assertRaisesRegex(ValueError, "Remote project changed"):
                update_interface(api, creator.state, directory, view)
            self.assertEqual(api.rows["/projects"][0]["metadata"], {"manual_edit": "preserve"})
            self.assertFalse(any(call[0] == "PUT" for call in api.calls))


if __name__ == "__main__":
    unittest.main()
