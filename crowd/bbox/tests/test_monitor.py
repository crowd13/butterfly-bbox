"""Continuous scoring against a fake API; no live pool operations."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crowd.bbox.api import ApiError
from crowd.bbox.monitor import check_once, watch
from crowd.bbox.staging import stage
from crowd.bbox.storage import RunBusyError, locked
from crowd.bbox.tests.test_bbox import FakeApi, inputs
from crowd.bbox.workflow import Run, prepare


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        prepare(self.path, *inputs())
        self.api = FakeApi()
        self.run = Run(self.path, self.api)
        self.run.create()

    def test_open_exam_scores_completed_answers_and_restart_is_idempotent(self):
        self.api.finish(self.run.state["batches"][0], ("w1", "w2"))
        self.api.objects["/pools"][0]["status"] = "OPEN"
        self.api.objects["/assignments"][-1]["status"] = "ACTIVE"
        with self.assertLogs("crowd.bbox.monitor", level="INFO") as logs:
            check_once(self.path, self.api)
        self.assertTrue(any("результат 100%" in line for line in logs.output))
        state = Run(self.path).state
        self.assertEqual(state["workers"]["w1"]["access"], 100)
        self.assertNotIn("w2", state["workers"])
        with patch.object(self.api, "skill", wraps=self.api.skill) as skill:
            check_once(self.path, self.api)
            skill.assert_not_called()
        self.api.objects["/assignments"][-1]["status"] = "ACCEPTED"
        check_once(self.path, self.api)
        self.assertEqual(Run(self.path).state["workers"]["w2"]["access"], 100)

    def test_two_early_control_errors_revoke_access_without_repeated_writes(self):
        self.api.finish(self.run.state["batches"][0], ("w1", "w2", "w3", "w4"))
        check_once(self.path, self.api)
        run = Run(self.path, self.api)
        run.next_batch()
        batch = run.state["batches"][-1]
        self.api.finish(batch, wrong_controls=True)
        work = [a for a in self.api.objects["/assignments"] if a["pool_id"] == batch["pool_id"]]
        for answer in work[2:]:
            answer["status"] = "ACTIVE"
        with self.assertLogs("crowd.bbox.monitor", level="INFO") as logs:
            check_once(self.path, self.api)
        self.assertTrue(any("допуск снят" in line for line in logs.output))
        state = Run(self.path).state
        self.assertEqual(state["workers"]["w1"]["access"], 0)
        self.assertEqual(len(state["events"]), 2)
        with patch.object(self.api, "skill", wraps=self.api.skill) as skill:
            check_once(self.path, self.api)
            skill.assert_not_called()
        self.assertFalse(any(path.endswith("/open") for _, path, _ in self.api.calls))

    def test_transient_failure_retries_and_stop_is_logged(self):
        with (
            patch(
                "crowd.bbox.monitor.check_once", side_effect=[ApiError("timeout"), None]
            ) as check,
            patch("crowd.bbox.monitor.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep,
            self.assertLogs("crowd.bbox.monitor", level="INFO") as logs,
            self.assertRaises(KeyboardInterrupt),
        ):
            watch(self.path, self.api)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(sleep.call_args.args, (30,))
        self.assertTrue(any("Проверка не завершена" in line for line in logs.output))
        self.assertTrue(any("Автопроверка остановлена" in line for line in logs.output))

    def test_manually_opened_staged_pool_controls_are_scored(self):
        stage(self.run)
        self.api.finish(self.run.state["batches"][0])
        pilot = self.run.state["staged_batches"][0]
        self.api.finish(pilot, wrong_controls=True)
        for pool in self.api.objects["/pools"]:
            if pool["id"] == pilot["pool_id"]:
                pool["status"] = "OPEN"
        check_once(self.path, self.api)
        state = Run(self.path).state
        self.assertEqual(state["workers"]["w1"]["access"], 0)
        self.assertTrue(state["staged_batches"][0]["control_report"]["w1"]["failed"])
        self.assertEqual(len(state["events"]), 10)
        self.assertEqual(len(state["batches"]), 1)

    def test_partial_skill_update_recovers_on_next_cycle(self):
        self.api.finish(self.run.state["batches"][0])
        original = self.api.skill

        def fail_access(skill_id, worker, value):
            if skill_id == self.run.state["resources"]["access"]:
                raise ApiError("timeout")
            original(skill_id, worker, value)

        with patch.object(self.api, "skill", side_effect=fail_access), self.assertRaises(ApiError):
            check_once(self.path, self.api)
        self.assertFalse(Run(self.path).state["exam_scores"])
        check_once(self.path, self.api)
        self.assertEqual(Run(self.path).state["workers"]["w1"]["access"], 100)

    def test_busy_run_is_retried(self):
        with (
            locked(self.path),
            patch("crowd.bbox.monitor.time.sleep", side_effect=KeyboardInterrupt),
            self.assertLogs("crowd.bbox.monitor", level="INFO") as logs,
            self.assertRaises(KeyboardInterrupt),
        ):
            watch(self.path, self.api)
        self.assertTrue(any("занят другой командой" in line for line in logs.output))

    def test_permanent_api_error_stops(self):
        with (
            patch("crowd.bbox.monitor.check_once", side_effect=ApiError("unauthorized", 401)),
            patch("crowd.bbox.monitor.time.sleep") as sleep,
            self.assertLogs("crowd.bbox.monitor"),
            self.assertRaises(ApiError),
        ):
            watch(self.path, self.api)
        sleep.assert_not_called()

    def test_locks_allow_cli_between_cycles_and_prevent_duplicate_watchers(self):
        def sleep(_):
            with locked(self.path):
                pass
            with self.assertRaises(RunBusyError):
                watch(self.path, self.api)
            raise KeyboardInterrupt

        with (
            patch("crowd.bbox.monitor.time.sleep", side_effect=sleep),
            self.assertLogs("crowd.bbox.monitor"),
            self.assertRaises(KeyboardInterrupt),
        ):
            watch(self.path, self.api)
        with self.assertRaises(ValueError):
            watch(self.path, self.api, interval=0)
