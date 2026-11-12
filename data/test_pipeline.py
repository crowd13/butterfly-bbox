"""Offline checks for license safety, review completeness and image validation."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from collect import photo_candidate
from contact_sheet import build_rows
from download import inspect_image
from finalize import finalize


class PipelineTests(unittest.TestCase):
    def test_each_photo_license_is_checked_not_observation_license(self):
        observation = {"id": 1, "license_code": "cc0", "user": {}, "taxon": {}, "photos": [
            {"id": 10, "license_code": "cc-by", "url": "https://example.org/photos/10/square.jpg"},
            {"id": 11, "license_code": "cc0", "url": "https://example.org/photos/11/square.png"},
        ]}
        source = {"taxon_id": 47224, "name": "butterflies", "proposed_class": "butterfly"}
        candidate = photo_candidate(observation, source, set(), set())
        self.assertEqual(candidate["photo_id"], 11)
        self.assertEqual(candidate["image_url"], "https://example.org/photos/11/original.png")
        self.assertIsNone(photo_candidate(observation, source, {11}, set()))
        self.assertIsNone(photo_candidate(observation, source, set(), {1}))

    def test_review_required_and_extra_photos_are_not_truncated(self):
        candidates = [{"photo_id": str(i), "license_code": "cc0"} for i in range(405)]
        downloads = [{"photo_id": str(i), "status": "ok", "width": "100", "height": "80", "exif_orientation": "1"} for i in range(405)]
        decisions = [{"photo_id": str(i), "decision": "keep", "class": "butterfly", "groups": "cropped;multiple"} for i in range(405)]
        result = finalize(candidates, downloads, decisions)
        self.assertEqual(len(result), 405)
        self.assertEqual(result[0]["groups"], "cropped;multiple")
        decisions[0]["decision"] = "pending"
        with self.assertRaisesRegex(ValueError, "unfinished"):
            finalize(candidates, downloads, decisions)

    def test_failed_download_is_not_a_negative_example(self):
        candidate = {"photo_id": "1", "license_code": "cc0"}
        decision = {"photo_id": "1", "decision": "keep", "class": "unrelated"}
        with self.assertRaisesRegex(ValueError, "unsuccessfully downloaded"):
            finalize([candidate], [{"photo_id": "1", "status": "error"}], [decision])

    def test_duplicate_or_missing_decisions_cannot_be_frozen(self):
        candidate = {"photo_id": "1", "license_code": "cc0"}
        with self.assertRaisesRegex(ValueError, "repeated"):
            finalize([candidate, candidate], [], [])
        with self.assertRaisesRegex(ValueError, "every candidate"):
            finalize([candidate], [], [])

    def test_decode_and_orientation_match_browser_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "1.jpg"
            image = Image.new("RGB", (40, 20), "green")
            exif = image.getexif()
            exif[274] = 6
            image.save(path, exif=exif)
            self.assertEqual(inspect_image(path), (20, 40, 6))
            candidates = [{"photo_id": "1"}]
            rows = build_rows(candidates, [{"photo_id": "1", "status": "ok", "local_path": str(path)}], Path(directory) / "review.html")
            with Image.open(Path(directory) / rows[0]["thumbnail"]) as thumb:
                self.assertEqual(thumb.size, (20, 40))
            path.write_bytes(b"not an image")
            with self.assertRaises(Exception):
                inspect_image(path)


if __name__ == "__main__":
    unittest.main()
