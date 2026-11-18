import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from crowd.review.audit import audit, main
from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json
from crowd.review.project import build


def assignments_for(plan, stage):
    result = []
    for index, suite in enumerate(plan[stage]):
        for worker in ("reviewer-1", "reviewer-2"):
            result.append({
                "id": f"{stage}:{index}:{worker}", "pool_id": stage, "status": "ACCEPTED",
                "user_id": worker,
                "tasks": [{"input_values": copy.deepcopy(task["input_values"])}
                          for task in suite["tasks"]],
                "solutions": [{"output_values": copy.deepcopy(
                    task.get("known_solutions", [{"output_values": {"verdict": "OK"}}])[0][
                        "output_values"
                    ]
                )} for task in suite["tasks"]],
            })
    return result


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build()
        cls.source = cls.plan["source"]
        cls.resources = {"pilot": "pilot", "main": "main"}

    def test_pilot_completion_does_not_claim_main_or_full_completion(self):
        rows = assignments_for(self.plan, "pilot")
        result = audit(rows, self.source, self.plan, self.resources)
        self.assertFalse(result["ready"])
        self.assertTrue(result["pools"]["pilot"]["ready"])
        self.assertEqual(result["pools"]["pilot"]["two_independent_reviews"], 40)
        self.assertEqual(result["pools"]["main"]["expected_photos"], 360)
        self.assertEqual(len(result["pools"]["main"]["missing_photo_ids"]), 360)
        self.assertEqual(result["assignment_statuses"], {"ACCEPTED": 16})

    def test_partial_pilot_uses_frozen_expected_ids_and_low_control_n_is_not_failure(self):
        rows = assignments_for(self.plan, "pilot")[:2]
        control = next(
            index for index, task in enumerate(self.plan["pilot"][0]["tasks"])
            if "known_solutions" in task
        )
        wanted = rows[0]["solutions"][control]["output_values"]["verdict"]
        rows[0]["solutions"][control]["output_values"]["verdict"] = (
            "EXTRA" if wanted != "EXTRA" else "OK"
        )
        result = audit(rows, self.source, self.plan, self.resources)
        pilot = result["pools"]["pilot"]
        self.assertFalse(pilot["ready"])
        self.assertEqual(pilot["two_independent_reviews"], 5)
        self.assertEqual(len(pilot["missing_photo_ids"]), 35)
        stats = result["control_quality"]["reviewer-1"]
        self.assertEqual((stats["correct"], stats["total"], stats["accuracy"]), (0, 1, 0))
        self.assertEqual(stats["assessment"], "insufficient_data")
        self.assertNotIn("passed", stats)

    def test_complete_dataset_preserves_uncertain_and_disagreement_for_manual_review(self):
        rows = assignments_for(self.plan, "pilot") + assignments_for(self.plan, "main")
        first = next(index for index, task in enumerate(rows[0]["tasks"])
                     if not task["input_values"]["answer_id"].startswith("gold:"))
        rows[0]["solutions"][first]["output_values"]["verdict"] = "BOUNDARY"
        result = audit(rows, self.source, self.plan, self.resources)
        self.assertTrue(result["ready"])
        self.assertEqual(sum(
            pool["two_independent_reviews"] for pool in result["pools"].values()
        ), 400)
        self.assertEqual(result["pools"]["pilot"]["disagreements"], 1)
        uncertain = {
            row["answer_id"] for row in self.source if row["solution"]["status"] == "uncertain"
        }
        manual = {
            row["answer_id"] for pool in result["pools"].values() for row in pool["manual_cases"]
        }
        self.assertTrue(uncertain <= manual)
        self.assertEqual(sum(result["decision_counts"].values()), 400)

    def test_self_review_duplicate_worker_and_changed_input_block_readiness(self):
        for change in ("self", "same_worker", "input", "duplicate"):
            with self.subTest(change=change):
                rows = assignments_for(self.plan, "pilot")
                if change == "self":
                    key = next(task["input_values"]["answer_id"] for task in rows[0]["tasks"]
                               if not task["input_values"]["answer_id"].startswith("gold:"))
                    rows[0]["user_id"] = next(row["worker_id"] for row in self.source
                                               if row["answer_id"] == key)
                elif change == "same_worker":
                    rows[1]["user_id"] = rows[0]["user_id"]
                elif change == "input":
                    rows[0]["tasks"][0]["input_values"]["width"] += 1
                else:
                    rows.append(copy.deepcopy(rows[0]))
                result = audit(rows, self.source, self.plan, self.resources)
                self.assertFalse(result["pools"]["pilot"]["ready"])
                self.assertFalse(result["ready"])

    def test_rejected_answers_do_not_count_and_wrong_stage_is_not_accepted(self):
        rows = assignments_for(self.plan, "pilot")
        rows[0]["status"] = "REJECTED"
        result = audit(rows, self.source, self.plan, self.resources)
        self.assertEqual(result["assignment_statuses"]["REJECTED"], 1)
        self.assertEqual(result["pools"]["pilot"]["two_independent_reviews"], 35)
        rows[0]["status"] = "ACCEPTED"
        rows[0]["pool_id"] = "main"
        result = audit(rows, self.source, self.plan, self.resources)
        self.assertTrue(result["pools"]["main"]["errors"])
        self.assertFalse(result["ready"])

    def test_cli_preserves_old_full_export_but_marks_it_stale_when_only_pilot_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "assignments.json", assignments_for(self.plan, "pilot"))
            write_json(root / "source.json", self.source)
            write_json(root / "snapshot.json", {"plan": self.plan})
            write_json(root / "state.json", {"resources": self.resources})
            write_json(root / "reviews.json", {"old": True})
            with patch("sys.argv", ["audit", "--run", str(root)]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 2)
            report = read_json(root / "audit.json")
            self.assertFalse(report["ready"])
            self.assertFalse(report["artifacts"]["reviews"]["current"])
            self.assertTrue(report["artifacts"]["pilot_reviews"]["current"])
            self.assertEqual(read_json(root / "reviews.json"), {"old": True})
            self.assertEqual(len(read_json(root / "pilot-reviews.json")), 40)
            self.assertEqual(len(report["input_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
