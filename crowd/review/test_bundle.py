import copy
import csv
import json
import re
import tempfile
import unittest
from pathlib import Path

from crowd.review.bundle import export_bundle
from crowd.review.project import build
from crowd.review.render import project_body


class BundleTests(unittest.TestCase):
    def test_json_matches_api_payload_and_keeps_author_exclusions(self):
        plan = build()
        project = project_body("bundle:test")
        before = copy.deepcopy(plan)
        with tempfile.TemporaryDirectory() as root:
            directory = export_bundle(plan, Path(root) / "bundle", project)
            self.assertEqual(json.loads((directory / "project.json").read_text()), project)
            for name in ("exam", "pilot", "main"):
                suites = json.loads((directory / (name + ".json")).read_text())
                self.assertEqual(suites, plan[name])
                self.assertTrue(all("pool_id" not in suite for suite in suites))
            training = json.loads((directory / "training.json").read_text())
            for task, row in zip(training, plan["examples"]["practice"], strict=True):
                self.assertNotIn("pool_id", task)
                self.assertEqual(
                    task["known_solutions"][0]["output_values"],
                    {"verdict": row["verdict"]},
                )
                self.assertEqual(task["message_on_unknown_solution"], row["hint"])
                self.assertTrue(task["infinite_overlap"])
            self.assertFalse((directory / "pilot.tsv").exists())
            self.assertFalse((directory / "main.tsv").exists())
            self.assertEqual(
                (directory / "interface.js").read_text(),
                project["task_spec"]["view_spec"]["script"],
            )
            self.assertEqual(
                json.loads((directory / "settings.json").read_text()),
                {"config": plan["config"], "budget": plan["budget"]},
            )
        self.assertEqual(plan, before)

    def test_training_tsv_preserves_arrays_and_unicode_hints(self):
        plan = build()
        boxes = [
            {"left": 0.1, "top": 0.2, "width": 0.3, "height": 0.4},
            {"left": 0.5, "top": 0.6, "width": 0.1, "height": 0.2},
        ]
        practice = plan["examples"]["practice"]
        practice[0]["source_boxes"] = []
        practice[1]["source_boxes"] = boxes
        practice[1]["hint"] = 'Проверьте молей, крылья и "усики".\nЗатем\tвыберите ответ.'
        with tempfile.TemporaryDirectory() as root:
            directory = export_bundle(plan, root, project_body("bundle:test"))
            with (directory / "training.tsv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
        self.assertEqual(len(rows), len(practice))
        self.assertIn("GOLDEN:verdict", rows[0])
        self.assertIn("HINT:text", rows[0])
        self.assertNotIn("HINT:verdict", rows[0])
        self.assertEqual(rows[0]["INPUT:source_boxes"], "")
        cell = rows[1]["INPUT:source_boxes"]
        self.assertFalse(cell.startswith("["))
        decoded = [json.loads(value.replace("\\,", ",")) for value in re.split(r"(?<!\\),", cell)]
        self.assertEqual(decoded, boxes)
        self.assertEqual(rows[1]["HINT:text"], practice[1]["hint"])
        self.assertEqual(rows[1]["GOLDEN:verdict"], practice[1]["verdict"])


if __name__ == "__main__":
    unittest.main()
