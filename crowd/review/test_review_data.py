import copy
import unittest

from crowd.review.export import export
from crowd.review.prepare import pages, public_input, read_json, validate_answer
from crowd.review.project import DEFAULT_SOURCE, build


def answer():
    return {
        "answer_id": "answer-1",
        "photo_id": "1",
        "worker_id": "author",
        "image_url": "https://example.org/1.jpg",
        "width": 640,
        "height": 480,
        "solution": {
            "status": "boxes",
            "boxes": [{"left": 0.1, "top": 0.2, "width": 0.3, "height": 0.4}],
        },
    }


def assignment(key, worker, row, verdict="OK"):
    return {
        "id": key,
        "status": "ACCEPTED",
        "user_id": worker,
        "tasks": [{"input_values": public_input(row)}],
        "solutions": [{"output_values": {"verdict": verdict}}],
    }


class ReviewDataTests(unittest.TestCase):
    def test_invalid_boxes_and_dimensions_cannot_reach_task_upload(self):
        for value in (float("nan"), float("inf"), True, None):
            with self.subTest(coordinate=value):
                row = answer()
                row["solution"]["boxes"][0]["left"] = value
                with self.assertRaises(ValueError):
                    validate_answer(row)
        for value in (0, -1, 1.5, True, "640"):
            with self.subTest(width=value):
                row = answer()
                row["width"] = value
                with self.assertRaises(ValueError):
                    validate_answer(row)
        row = answer()
        row["solution"]["status"] = "no_butterflies"
        with self.assertRaisesRegex(ValueError, "contradict"):
            validate_answer(row)

    def test_source_ids_are_unique_after_normalization(self):
        first, second = answer(), answer()
        second["photo_id"] = 1
        second["answer_id"] = "answer-2"
        config = {"dataset_size": 2, "seed": 42, "work_per_page": 1}
        with self.assertRaisesRegex(ValueError, "unique answers"):
            pages([first, second], [], config)
        second["photo_id"] = "2"
        second["answer_id"] = first["answer_id"]
        with self.assertRaisesRegex(ValueError, "unique answers"):
            pages([first, second], [], config)

    def test_control_position_varies_reproducibly(self):
        plan = build()
        suites = plan["pilot"] + plan["main"]
        positions = {
            next(i for i, task in enumerate(suite["tasks"]) if "known_solutions" in task)
            for suite in suites
        }
        self.assertEqual(positions, set(range(6)))
        again = build()
        self.assertEqual(suites, again["pilot"] + again["main"])

    def test_unresolved_source_is_sent_to_manual_review_even_after_two_ok(self):
        row = answer()
        row["solution"] = {"status": "uncertain", "boxes": []}
        result = export(
            [assignment("a", "r1", row), assignment("b", "r2", row)], [row]
        )
        self.assertEqual(result[0]["decision"], "MANUAL")

    def test_rejected_or_duplicate_reviews_cannot_complete_a_pair(self):
        row = answer()
        first = assignment("a", "r1", row)
        second = assignment("b", "r2", row)
        second["status"] = "REJECTED"
        with self.assertRaisesRegex(ValueError, "two independent"):
            export([first, second], [row])
        with self.assertRaisesRegex(ValueError, "Duplicate assignment"):
            export([first, copy.deepcopy(first)], [row])
        with self.assertRaisesRegex(ValueError, "two independent"):
            export([first, assignment("b", "r1", row)], [row])

    def test_malformed_review_cannot_be_attached_to_another_photo(self):
        row = answer()
        first = assignment("a", "r1", row)
        second = assignment("b", "r2", row)
        second["tasks"][0]["input_values"]["photo_id"] = "wrong-photo"
        with self.assertRaisesRegex(ValueError, "does not match"):
            export([first, second], [row])
        second = assignment("b", "r2", row)
        second["tasks"][0]["input_values"]["answer_id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Unknown reviewed answer"):
            export([first, second], [row])

    def test_duplicate_source_and_missing_reviewer_are_rejected(self):
        row = answer()
        with self.assertRaisesRegex(ValueError, "Source answer and photo IDs"):
            export([], [row, copy.deepcopy(row)])
        with self.assertRaisesRegex(ValueError, "Missing reviewer"):
            export([assignment("a", "", row)], [row])

    def test_reviews_cannot_approve_changed_source_data(self):
        original = answer()
        assignments = copy.deepcopy(
            [assignment("a", "r1", original), assignment("b", "r2", original)]
        )
        changes = [
            {"solution": {"status": "no_butterflies", "boxes": []}},
            {"solution": {"status": "uncertain", "boxes": []}},
            {"width": 1280},
            {"height": 960},
            {"image_url": "https://example.org/replaced.jpg"},
        ]
        changed_boxes = copy.deepcopy(original["solution"])
        changed_boxes["boxes"][0]["left"] = 0.5
        changes.append({"solution": changed_boxes})
        for change in changes:
            with self.subTest(change=change):
                changed = {**original, **change}
                with self.assertRaisesRegex(ValueError, "Reviewed input does not match"):
                    export(assignments, [changed])

    def test_reviews_with_missing_input_fields_are_rejected(self):
        row = answer()
        assignments = [assignment("a", "r1", row), assignment("b", "r2", row)]
        del assignments[1]["tasks"][0]["input_values"]["source_boxes"]
        with self.assertRaisesRegex(ValueError, "Reviewed input does not match"):
            export(assignments, [row])

    def test_full_dataset_flow_preserves_controls_and_unresolved_cases(self):
        plan = build()
        assignments = []
        for index, suite in enumerate(plan["pilot"] + plan["main"]):
            for worker in ("reviewer-1", "reviewer-2"):
                assignments.append(
                    {
                        "id": f"{index}:{worker}",
                        "status": "ACCEPTED",
                        "user_id": worker,
                        "tasks": [
                            {"input_values": task["input_values"]} for task in suite["tasks"]
                        ],
                        "solutions": [
                            {"output_values": {"verdict": "OK"}} for _ in suite["tasks"]
                        ],
                    }
                )
        result = export(assignments, read_json(DEFAULT_SOURCE))
        self.assertEqual(len(result), 400)
        self.assertEqual(sum(row["decision"] == "MANUAL" for row in result), 2)


if __name__ == "__main__":
    unittest.main()
