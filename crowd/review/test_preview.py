import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from crowd.review.project import build
from crowd.review.render import assets, preview


class Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.ids.append(dict(attrs)["id"])


class PreviewTests(unittest.TestCase):
    def test_practice_contains_only_public_examples_and_no_exam_or_work_data(self):
        plan = build()
        script = assets(plan["examples"]["practice"])["script"]
        practice = json.loads(re.search(r"var reviewPractice = (.+);", script)[1])
        self.assertEqual(len(practice), 6)
        self.assertTrue(all(set(row) == {"image_url", "source_boxes", "verdict", "hint"} for row in practice))
        for row in plan["examples"]["exam"] + plan["source"]:
            self.assertNotIn(row["image_url"], script)

    def render(self, inputs):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.html"
            preview(inputs, output)
            return output.read_text(encoding="utf-8")

    def test_six_independent_cards_without_solutions(self):
        inputs = [
            {
                "photo_id": str(index),
                "image_url": f"https://example.org/{index}.jpg",
                "source_boxes": [],
                "verdict": "SECRET_VERDICT",
                "hint": "SECRET_HINT",
                "known_solutions": [{"output_values": {"verdict": "SECRET"}}],
            }
            for index in range(6)
        ]
        page = self.render(inputs)
        cards = Cards()
        cards.feed(page)
        tasks = json.loads(re.search(r"var previewTasks = (.+);", page)[1])
        self.assertEqual(cards.ids, [f"preview-task-{i}" for i in range(1, 7)])
        self.assertEqual([task["id"] for task in tasks], cards.ids)
        self.assertEqual(len({task["id"] for task in tasks}), 6)
        self.assertTrue(all("source_boxes" in task["input_values"] for task in tasks))
        self.assertNotIn("SECRET", page)
        self.assertIn("Инструкция", page)
        self.assertIn("tasks.map(function(task)", page)

    def test_single_card_and_script_safe_inputs(self):
        page = self.render({"photo_id": "</script><script>sentinel", "source_boxes": []})
        cards = Cards()
        cards.feed(page)
        self.assertEqual(cards.ids, ["preview-task-1"])
        self.assertNotIn("</script><script>sentinel", page)
        tasks = json.loads(re.search(r"var previewTasks = (.+);", page)[1])
        self.assertEqual(tasks[0]["input_values"]["photo_id"], "</script><script>sentinel")


if __name__ == "__main__":
    unittest.main()
