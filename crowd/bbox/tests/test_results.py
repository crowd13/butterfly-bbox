"""Viewer serialization must preserve answers without permitting HTML injection."""

import json
import tempfile
import unittest
from pathlib import Path

from crowd.bbox.results import write_results


class ResultsTests(unittest.TestCase):
    def test_untrusted_strings_stay_inside_json(self):
        rows = [{"photo_id": "</script><script>alert(1)</script>", "solution": None}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            source = Path(directory) / "result.json"
            source.write_text(json.dumps(rows))
            write_results(source, path)
            page = path.read_text()
        embedded = page.split('<script id="data" type="application/json">')[1].split("</script>")[0]
        self.assertNotIn("<script>", embedded)
        self.assertEqual(json.loads(embedded), rows)
        self.assertNotIn("__RESULT_DATA__", page)
