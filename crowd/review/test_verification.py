import copy
import json
import tempfile
import unittest
from pathlib import Path

from crowd.review.render import project_body
from crowd.review.verification import differences, normalize_project, verify_resources


class VerificationTests(unittest.TestCase):
    def test_actual_server_list_formatting_keeps_the_same_instruction(self):
        fixture = Path(__file__).parent / "tests/fixtures/instructions.json"
        pair = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertNotEqual(pair["actual"], pair["expected"])
        self.assertEqual(differences(pair["actual"], pair["expected"], "public_instructions"), [])
        changed = pair["actual"].replace("молей и мотыльков", "только дневных бабочек")
        self.assertTrue(differences(changed, pair["expected"], "public_instructions"))

    def test_instruction_text_inline_spaces_attributes_and_order_stay_significant(self):
        for expected, actual in (
            ("<p><b>А</b> <b>Б</b></p>", "<p><b>А</b><b>Б</b></p>"),
            ("<pre>А\n  Б</pre>", "<pre>А Б</pre>"),
            ("<ul><li>А</li><li>Б</li></ul>", "<ul><li>Б</li><li>А</li></ul>"),
            ('<a href="a">А</a>', '<a href="b">А</a>'),
        ):
            with self.subTest(expected=expected):
                self.assertTrue(differences(actual, expected, "public_instructions"))

    def test_documented_default_omissions_and_enum_order_are_equivalent(self):
        expected = project_body("review:test")
        actual = copy.deepcopy(expected)
        actual.pop("assignments_issuing_type")
        actual.pop("assignments_automerge_enabled")
        for kind in ("input_spec", "output_spec"):
            for field in actual["task_spec"][kind].values():
                field.pop("hidden", None)
                field.pop("required", None)
        actual["task_spec"]["view_spec"].pop("settings")
        actual["task_spec"]["output_spec"]["verdict"]["allowed_values"].reverse()
        self.assertEqual(differences(normalize_project(actual), normalize_project(expected)), [])
        self.assertNotIn("hidden", expected["task_spec"]["input_spec"]["image_url"])

    def test_meaningful_schema_and_interface_changes_are_still_rejected(self):
        expected = project_body("review:test")
        for field, value in (("hidden", True), ("required", False), ("type", "integer")):
            actual = copy.deepcopy(expected)
            actual["task_spec"]["input_spec"]["answer_id"][field] = value
            diff = differences(normalize_project(actual), normalize_project(expected))
            self.assertEqual(diff[0]["field"], f"task_spec.input_spec.answer_id.{field}")
        actual = copy.deepcopy(expected)
        actual["task_spec"]["view_spec"]["script"] += "\n"
        diff = differences(normalize_project(actual), normalize_project(expected))
        self.assertEqual(diff[0]["field"], "task_spec.view_spec.script")
        self.assertEqual(diff[0]["actual"]["length"], len(actual["task_spec"]["view_spec"]["script"]))
        actual = copy.deepcopy(expected)
        actual["task_spec"]["input_spec"]["surprise"] = {"type": "string", "required": True}
        diff = differences(normalize_project(actual), normalize_project(expected))
        self.assertEqual(diff[0], {"field": "task_spec.input_spec.surprise", "unexpected": True})

    def test_report_identifies_all_resources_and_exact_fields(self):
        class Api:
            def request(self, method, path):
                if path == "/projects/p":
                    return {"public_name": "changed", "status": "ACTIVE"}
                return {"reward_per_assignment": 2, "status": "CLOSED"}

        state = {
            "resources": {"project": "p", "main": "m"},
            "definitions": {
                "project": {"public_name": "expected"},
                "main": {"reward_per_assignment": 0.75},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "verification.json"
            with self.assertRaisesRegex(ValueError, "project.public_name, main.reward"):
                verify_resources(Api(), state, report)
            result = json.loads(report.read_text())
        self.assertFalse(result["verified"])
        self.assertEqual(len(result["mismatches"]), 2)


if __name__ == "__main__":
    unittest.main()
