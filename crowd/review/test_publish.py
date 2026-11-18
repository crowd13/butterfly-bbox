import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.review.interface_update import write_json
from crowd.review.prepare import read_json
from crowd.review.project import build
from crowd.review.publish import bundle_readme, checksum, export_review_bundle
from crowd.review.test_audit import assignments_for


class PublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build()

    def run_files(self, root, stages=("pilot",)):
        run = Path(root) / "run"
        rows = [row for stage in stages for row in assignments_for(self.plan, stage)]
        for row in rows:
            row.update(reward="0.75", owner={"id": "team"})
        write_json(run / "assignments.json", rows)
        write_json(run / "source.json", self.plan["source"])
        write_json(run / "snapshot.json", {"plan": self.plan})
        write_json(run / "state.json", {
            "run_id": "test-run", "requester_id": "team", "source_project_id": "11779",
            "resources": {"project": "11912", "training": "training", "exam": "exam",
                          "pilot": "pilot", "main": "main"},
        })
        return run, rows

    def test_partial_pilot_preserves_all_source_and_marks_every_missing_photo_pending(self):
        with tempfile.TemporaryDirectory() as root:
            run, rows = self.run_files(root)
            rejected = {**copy.deepcopy(rows[0]), "id": "rejected", "status": "REJECTED"}
            rows.append(rejected)
            write_json(run / "assignments.json", rows)
            output = Path(root) / "bundle"
            result = export_review_bundle(run, output)
            self.assertEqual(len(read_json(output / "source.json")), 400)
            self.assertEqual(len(read_json(output / "reviewed-source.json")), 40)
            self.assertEqual(len(read_json(output / "reviews.json")), 40)
            self.assertEqual(read_json(output / "assignments.json"), rows)
            photos = [
                json.loads(line) for line in (output / "photos.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(photos), 400)
            self.assertEqual(sum(
                row["effective_decision"] == "PENDING_REVIEW" for row in photos
            ), 360)
            pending = read_json(output / "pending.json")
            self.assertEqual(sum(
                "MISSING_INDEPENDENT_REVIEWS" in row["reasons"] for row in pending
            ), 360)
            self.assertFalse(result["golden_ready"])
            self.assertFalse(result["review_collection_complete"])
            costs = read_json(output / "report.json")["expenses"]
            self.assertEqual(costs["actual_assignment_rewards_rub"], "12.00")
            self.assertEqual(costs["estimated_total_with_commission_rub"], "16.80")
            self.assertIsNone(costs["actual_commission_rub"])
            self.assertFalse(costs["exam_included"])
            self.assertEqual(result["expenses"], costs)
            self.assertEqual(result["input_sha256"], {
                name: checksum(run / name) for name in (
                    "state.json", "snapshot.json", "source.json", "assignments.json"
                )
            })
            readme = (output / "README.md").read_text()
            self.assertIn("README.md", result["files_sha256"])
            self.assertIn(f"Фотографий: {result['source_photos']}", readme)
            self.assertIn(f"{result['reviewed_photos']}; в очереди: {result['pending_photos']}",
                          readme)
            self.assertIn(f"{costs['actual_assignment_rewards_rub']} ₽", readme)
            self.assertIn("golden_ready=false", readme)
            review_directory = Path(__file__).resolve().parent
            canonical_readme = bundle_readme(result, review_directory / "runs/final")
            self.assertIn("[review README](../../README.md)", canonical_readme)
            self.assertIn("[coverage.json](../../../bbox/runs/final/coverage.json)",
                          canonical_readme)
            for name, digest in result["files_sha256"].items():
                self.assertEqual(checksum(output / name), digest)

    def test_ai_false_accept_flag_creates_manual_queue_without_changing_raw_votes_or_boxes(self):
        with tempfile.TemporaryDirectory() as root:
            run, rows = self.run_files(root)
            completed = {
                task["input_values"]["answer_id"] for row in rows for task in row["tasks"]
            }
            source = next(row for row in self.plan["source"] if row["answer_id"] in completed
                          and row["solution"]["status"] != "uncertain")
            flag = {"answer_id": source["answer_id"], "photo_id": source["photo_id"],
                    "source_solution": copy.deepcopy(source["solution"]),
                    "proposal_origin": "AI_VISUAL_REVIEW", "reason": "Suspected missed adult moth",
                    "status": "PENDING_HUMAN_REVIEW", "proposed_solution": {"boxes": []}}
            write_json(Path(root) / "known-qa-cases.json", {"cases": [flag]})
            output = Path(root) / "bundle"
            export_review_bundle(run, output)
            photo = next(
                json.loads(line) for line in (output / "photos.jsonl").read_text().splitlines()
                if json.loads(line)["answer_id"] == source["answer_id"]
            )
            self.assertEqual(photo["crowd_decision"], "ACCEPT")
            self.assertEqual(photo["effective_decision"], "MANUAL")
            self.assertEqual([vote["verdict"] for vote in photo["reviews"]], ["OK", "OK"])
            self.assertEqual(photo["source"], source)
            self.assertEqual(photo["qa_refs"], [0])
            pending = next(row for row in read_json(output / "pending.json")
                           if row["answer_id"] == source["answer_id"])
            self.assertIn("AI_QA_FLAG_REQUIRES_HUMAN_REVIEW", pending["reasons"])
            self.assertEqual(read_json(output / "assignments.json"), rows)

    def test_qa_id_photo_or_source_solution_mismatch_blocks_export(self):
        for change in ("unknown_id", "wrong_photo", "wrong_solution", "null_id"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                run, _ = self.run_files(root)
                source, other = self.plan["source"][:2]
                flag = {"answer_id": source["answer_id"], "photo_id": source["photo_id"],
                        "source_solution": copy.deepcopy(source["solution"])}
                if change == "unknown_id":
                    flag["answer_id"] = "unknown-answer"
                elif change == "wrong_photo":
                    flag["photo_id"] = other["photo_id"]
                elif change == "wrong_solution":
                    flag["source_solution"] = {"status": "invalid", "boxes": []}
                else:
                    flag["answer_id"] = None
                write_json(Path(root) / "known-qa-cases.json", {"cases": [flag]})
                output = Path(root) / "bundle"
                with self.assertRaisesRegex(ValueError, "QA flag"):
                    export_review_bundle(run, output)
                self.assertFalse(output.exists())

    def test_both_qa_sources_keep_all_flags_and_union_the_manual_queue(self):
        with tempfile.TemporaryDirectory() as root:
            run, rows = self.run_files(root)
            output = Path(root) / "bundle"
            before = export_review_bundle(run, output)
            raw_reviews = read_json(output / "reviews.json")
            completed = {row["answer_id"] for row in raw_reviews if row["decision"] == "ACCEPT"}
            source = [row for row in self.plan["source"] if row["answer_id"] in completed][:3]
            flags = [{"answer_id": row["answer_id"], "photo_id": row["photo_id"],
                      "source_solution": copy.deepcopy(row["solution"]),
                      "proposal_origin": "AI_VISUAL_REVIEW"} for row in source]
            known_path, pilot_path = Path(root) / "known-qa-cases.json", run / "pilot-qa.json"
            write_json(known_path, {"cases": flags[:2]})
            photo_only = copy.deepcopy(flags[1])
            photo_only.pop("answer_id")
            write_json(pilot_path, {"cases": [photo_only, flags[2]]})
            result = export_review_bundle(run, output)
            photos = {row["answer_id"]: row for row in (
                json.loads(line) for line in (output / "photos.jsonl").read_text().splitlines()
            )}
            self.assertEqual([photos[row["answer_id"]]["qa_refs"] for row in source],
                             [[0], [1, 2], [3]])
            self.assertTrue(all(photos[row["answer_id"]]["effective_decision"] == "MANUAL"
                                for row in source))
            exported_flags = read_json(output / "qa-flags.json")
            self.assertEqual(len(exported_flags), 4)
            self.assertTrue(all(flag["is_independent_human_review"] is False
                                for flag in exported_flags))
            self.assertEqual(exported_flags[2]["answer_id"], source[1]["answer_id"])
            self.assertEqual(result["pending_photos"], before["pending_photos"] + 3)
            self.assertEqual(result["qa_input_sha256"], {
                "../known-qa-cases.json": checksum(known_path), "pilot-qa.json": checksum(pilot_path)
            })
            self.assertEqual(read_json(output / "reviews.json"), raw_reviews)
            self.assertEqual(read_json(output / "assignments.json"), rows)

    def test_zero_or_one_review_stays_pending_and_keeps_partial_votes(self):
        for assignment_count in (0, 1):
            with self.subTest(assignment_count=assignment_count), tempfile.TemporaryDirectory() as root:
                run, rows = self.run_files(root)
                rows = rows[:assignment_count]
                write_json(run / "assignments.json", rows)
                output = Path(root) / "bundle"
                result = export_review_bundle(run, output)
                self.assertEqual(result["reviewed_photos"], 0)
                self.assertEqual(result["pending_photos"], result["source_photos"])
                self.assertEqual(read_json(output / "reviews.json"), [])
                photos = [json.loads(line) for line in
                          (output / "photos.jsonl").read_text().splitlines()]
                self.assertTrue(all(row["effective_decision"] == "PENDING_REVIEW"
                                    and row["crowd_decision"] is None for row in photos))
                expected_votes = sum(task["input_values"]["answer_id"] in {
                    row["answer_id"] for row in self.plan["source"]
                } for assignment in rows for task in assignment["tasks"])
                self.assertEqual(sum(len(row["reviews"]) for row in photos), expected_votes)
                self.assertFalse(result["review_collection_complete"])

    def test_missing_or_invalid_reward_blocks_export(self):
        for reward in (None, "", "not-a-number", "-0.75", "NaN", "Infinity"):
            with self.subTest(reward=reward), tempfile.TemporaryDirectory() as root:
                run, rows = self.run_files(root)
                if reward is None:
                    rows[0].pop("reward")
                else:
                    rows[0]["reward"] = reward
                write_json(run / "assignments.json", rows)
                output = Path(root) / "bundle"
                with self.assertRaisesRegex(ValueError, "reward"):
                    export_review_bundle(run, output)
                self.assertFalse(output.exists())

    def test_foreign_pool_is_rejected_even_when_its_owner_matches(self):
        with tempfile.TemporaryDirectory() as root:
            run, rows = self.run_files(root)
            rows[0]["pool_id"] = "11907-personal-pool"
            write_json(run / "assignments.json", rows)
            output = Path(root) / "bundle"
            with self.assertRaisesRegex(ValueError, "unknown_pool_assignments"):
                export_review_bundle(run, output)
            self.assertFalse(output.exists())

    def test_invalid_input_duplicates_and_reviewer_independence_block_atomic_replacement(self):
        for change in ("input", "duplicate", "reviewer"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                run, rows = self.run_files(root)
                output = Path(root) / "bundle"
                export_review_bundle(run, output)
                before = {path.name: path.read_bytes() for path in output.iterdir()}
                if change == "input":
                    rows[0]["tasks"][0]["input_values"]["width"] += 1
                elif change == "duplicate":
                    rows.append(copy.deepcopy(rows[0]))
                else:
                    rows[1]["user_id"] = rows[0]["user_id"]
                write_json(run / "assignments.json", rows)
                with self.assertRaisesRegex(ValueError, "Review validation failed"):
                    export_review_bundle(run, output)
                self.assertEqual(
                    {path.name: path.read_bytes() for path in output.iterdir()}, before
                )
                self.assertEqual(
                    read_json(Path(root) / "bundle-error.json")["status"], "EXPORT_FAILED"
                )

    def test_missing_mixed_or_wrong_assignment_owner_blocks_bound_export(self):
        for change in ("all_missing", "one_missing", "wrong"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                run, rows = self.run_files(root)
                if change == "all_missing":
                    for row in rows:
                        row.pop("owner")
                elif change == "one_missing":
                    rows[0].pop("owner")
                else:
                    rows[0]["owner"] = {"id": "personal"}
                write_json(run / "assignments.json", rows)
                output = Path(root) / "bundle"
                with self.assertRaisesRegex(ValueError, "Assignment owner differs"):
                    export_review_bundle(run, output)
                self.assertFalse(output.exists())

    def test_missing_or_invalid_bound_identity_blocks_export(self):
        for field in ("requester_id", "source_project_id"):
            for value in (None, "", "   ", 11779):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as root:
                    run, _ = self.run_files(root)
                    state = read_json(run / "state.json")
                    if value is None:
                        state.pop(field)
                    else:
                        state[field] = value
                    write_json(run / "state.json", state)
                    output = Path(root) / "bundle"
                    with self.assertRaisesRegex(ValueError, f"nonempty {field}"):
                        export_review_bundle(run, output)
                    self.assertFalse(output.exists())

    def test_failed_staging_rename_restores_previous_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            run, rows = self.run_files(root)
            output = Path(root) / "bundle"
            export_review_bundle(run, output)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            rows[0]["reward"] = "0.95"
            write_json(run / "assignments.json", rows)
            original_rename = Path.rename

            def fail_staging_rename(path, target):
                if path.name == "bundle" and path.parent.name.startswith(".bundle-"):
                    raise OSError("staging rename failed")
                return original_rename(path, target)

            with patch.object(Path, "rename", fail_staging_rename):
                with self.assertRaisesRegex(OSError, "staging rename failed"):
                    export_review_bundle(run, output)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)
            self.assertIn("staging rename failed", read_json(Path(root) / "bundle-error.json")["error"])

    def test_repeat_is_identical_clears_stale_files_and_keeps_external_human_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            run, _ = self.run_files(root)
            output = Path(root) / "bundle"
            ledger = Path(root) / "golden_set/corrections.json"
            write_json(ledger, {"human": "preserve"})
            export_review_bundle(run, output)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            (output / "obsolete.json").write_text("old")
            export_review_bundle(run, output)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)
            self.assertEqual(read_json(ledger), {"human": "preserve"})
            self.assertFalse((output / "corrections.json").exists())

    def test_full_collection_still_does_not_claim_a_golden_set(self):
        with tempfile.TemporaryDirectory() as root:
            run, _ = self.run_files(root, ("pilot", "main"))
            output = Path(root) / "bundle"
            result = export_review_bundle(run, output)
            self.assertTrue(result["review_collection_complete"])
            self.assertEqual(result["reviewed_photos"], 400)
            self.assertFalse(result["golden_ready"])
            self.assertFalse(result["is_golden_set"])
            self.assertEqual(len(read_json(output / "reviews.json")), 400)
            readme = (output / "README.md").read_text()
            self.assertIn("review_collection_complete=true", readme)
            self.assertIn(f"{result['reviewed_photos']}; в очереди: {result['pending_photos']}",
                          readme)


if __name__ == "__main__":
    unittest.main()
