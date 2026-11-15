"""Poll exam and control answers without opening pools or changing the workflow stage."""

import copy
import logging
import time

from .api import ApiError
from .storage import RunBusyError, locked
from .workflow import Run

LOG = logging.getLogger(__name__)


def check_once(directory, api):
    # Reload state each cycle so audit, close and next-batch can run between checks.
    with locked(directory):
        run = Run(directory, api)
        if not run.state["batches"]:
            raise ValueError("Create the exam pool before starting watch")
        previous_scores = copy.deepcopy(run.state["exam_scores"])
        previous_events = copy.deepcopy(run.state["events"])
        previous_workers = copy.deepcopy(run.state["workers"])
        assignments = run.sync()
        run.score_exam(assignments)
        for batch in run.state["batches"]:
            if batch["kind"] != "exam":
                run.score_controls(assignments, batch)
        changed = 0
        for assignment_id, score in run.state["exam_scores"].items():
            if previous_scores.get(assignment_id) == score:
                continue
            changed += 1
            worker = score["worker_id"]
            access = run.state["workers"][worker]["access"]
            LOG.info(
                "Экзамен %s: исполнитель %s, результат %.0f%%, допуск %s%s",
                assignment_id,
                worker,
                score["score"],
                access,
                "; нужен разбор" if any(r["correct"] is None for r in score["results"]) else "",
            )
        controls = 0
        for answer_id, event in run.state["events"].items():
            if previous_events.get(answer_id) == event:
                continue
            controls += 1
            verdict = {True: "верно", False: "ошибка", None: "нужен разбор"}[event["correct"]]
            LOG.info(
                "Ханипот %s: пул %s, исполнитель %s — %s",
                answer_id,
                event["pool_id"],
                event["worker_id"],
                verdict,
            )
        for worker, state in run.state["workers"].items():
            if state.get("failed") and not previous_workers.get(worker, {}).get("failed"):
                LOG.warning("Исполнитель %s: контроль провален, допуск снят", worker)
        LOG.info("Проверка завершена: экзаменов обновлено %d, ханипотов %d", changed, controls)


def watch(directory, api, interval=30):
    if interval <= 0:
        raise ValueError("Interval must be a positive number of seconds")
    # A separate lock prevents two watchers, without blocking the other CLI commands.
    with locked(directory / ".watch"):
        LOG.info("Автопроверка запущена: %s; интервал %d с; остановка Ctrl+C", directory, interval)
        try:
            while True:
                try:
                    check_once(directory, api)
                except RunBusyError:
                    LOG.info("Проект занят другой командой; повтор через %d с", interval)
                except ApiError as exc:
                    if exc.status is not None and exc.status != 429 and exc.status < 500:
                        raise
                    LOG.error(
                        "Проверка не завершена: %s. Повтор через %d с. "
                        "При длительном сбое закройте открытый пул.",
                        exc,
                        interval,
                    )
                time.sleep(interval)
        finally:
            LOG.warning(
                "Автопроверка остановлена. Пулы не закрываются автоматически; "
                "закройте открытый пул, если контроль не будет продолжен."
            )
