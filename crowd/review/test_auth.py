import io
import unittest
import urllib.error
from unittest.mock import patch

from crowd.bbox.api import ApiError, TasksApi


class AuthTests(unittest.TestCase):
    def test_empty_prompt_does_not_silently_reuse_another_accounts_environment_token(self):
        with patch.dict("os.environ", {"YANDEX_TASKS_OAUTH": "environment-test-value"}):
            for token in ("", " \t"):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    TasksApi(token)
            self.assertEqual(TasksApi(" supplied-test-value ").token, "supplied-test-value")
            self.assertEqual(TasksApi().token, "environment-test-value")

    def test_http_diagnostics_show_only_the_error_code_and_never_response_payload(self):
        error = urllib.error.HTTPError(
            "https://tasks.yandex.ru/api/v1/pools", 400, "Bad Request", {},
            io.BytesIO(b'{"code":"VALIDATION_ERROR","message":"private-body-value",'
                       b'"payload":{"name":{"code":"INVALID_VALUE","message":"private-body-value"}}}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as result:
                TasksApi("supplied-test-value").request("POST", "/skills", {"name": "test"})
        self.assertIn("VALIDATION_ERROR", str(result.exception))
        self.assertNotIn("private-body-value", str(result.exception))
        self.assertNotIn("supplied-test-value", str(result.exception))
        self.assertIn("name: INVALID_VALUE", str(result.exception))


if __name__ == "__main__":
    unittest.main()
