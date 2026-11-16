"""Prepare and create the independent bbox review project."""

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crowd.bbox.api import ApiError, TasksApi
from crowd.review.prepare import (
    budget,
    control_specs,
    fixtures,
    pages,
    public_input,
    read_json,
)
from crowd.review.render import preview, project_body

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT.parent / "bbox/runs/final/review.json"
DEFAULT_GOLD = ROOT.parent / "bbox/fixtures/gold/gold.json"


def build(source=DEFAULT_SOURCE, gold=DEFAULT_GOLD):
    config = read_json(ROOT / "config.json")
    specs = read_json(ROOT / "fixtures.json")
    bank = read_json(gold)
    examples = {name: fixtures(bank, rows) for name, rows in specs.items()}
    excluded = {
        row["photo_id"] for name in ("practice", "exam") for row in examples[name]
    }
    control_count = config["dataset_size"] // config["work_per_page"]
    examples["controls"] = fixtures(bank, control_specs(bank, excluded, control_count))
    suites = pages(read_json(source), examples["controls"], config)
    pilot_pages = config["pilot_size"] // config["work_per_page"]
    return {
        "config": config,
        "examples": examples,
        "exam": [
            {
                "tasks": [
                    {
                        "input_values": {
                            k: v for k, v in row.items() if k not in {"verdict", "hint"}
                        },
                        "known_solutions": [
                            {
                                "output_values": {"verdict": row["verdict"]},
                                "correctness_weight": 1.0,
                            }
                        ],
                        "message_on_unknown_solution": row["hint"],
                    }
                    for row in examples["exam"]
                ],
                "overlap": config["exam_attempts"],
                "infinite_overlap": False,
            }
        ],
        "pilot": suites[:pilot_pages],
        "main": suites[pilot_pages:],
        "budget": budget(config, len(suites)),
    }


class Creator:
    def __init__(self, directory, api=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.state = (
            read_json(self.path)
            if self.path.exists()
            else {"run_id": str(uuid.uuid4()), "resources": {}, "operations": {}}
        )
        self.api = api or TasksApi()
        self.plan = build()

    def save(self):
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def ensure(self, key, path, body, marker_field, marker):
        if key in self.state["resources"]:
            return self.state["resources"][key]
        matches = [
            row for row in self.api.items(path) if row.get(marker_field) == marker
        ]
        if len(matches) > 1:
            raise ValueError(f"Several remote resources match {marker}")
        resource = matches[0] if matches else self.api.request("POST", path, body)
        self.state["resources"][key] = resource["id"]
        self.save()
        return resource["id"]

    def pool_body(self, name, price, overlap, qualified):
        filters = [
            {"category": "profile", "key": "languages", "operator": "IN", "value": "RU"}
        ]
        if qualified:
            filters.extend(
                [
                    {
                        "category": "skill",
                        "key": self.state["resources"]["exam_skill"],
                        "operator": "GTE",
                        "value": self.plan["config"]["exam_pass"],
                    },
                    {
                        "category": "skill",
                        "key": self.state["resources"]["access_skill"],
                        "operator": "EQ",
                        "value": 100,
                    },
                ]
            )
        body = {
            "project_id": self.state["resources"]["project"],
            "private_name": name,
            "will_expire": (datetime.now(timezone.utc) + timedelta(days=14)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "reward_per_assignment": float(price),
            "assignment_max_duration_seconds": self.plan["config"][
                "assignment_seconds"
            ],
            "auto_accept_solutions": True,
            "auto_close_after_complete_delay_seconds": 0,
            "may_contain_adult_content": False,
            "filter": {"and": [{"or": [row]} for row in filters]},
            "defaults": {"default_overlap_for_new_task_suites": overlap},
        }
        if not qualified:
            body["quality_control"] = {
                "training_requirement": {
                    "training_pool_id": self.state["resources"]["training"],
                    "training_passing_skill_value": 100,
                }
            }
        return body

    def upload(self, key, pool_id, suites):
        operation_id = self.state["operations"].setdefault(key, str(uuid.uuid4()))
        self.save()
        try:
            operation = self.api.request("GET", f"/operations/{operation_id}")
        except ApiError as error:
            if error.status != 404:
                raise
            operation = self.api.request(
                "POST",
                "/task-suites",
                [{**suite, "pool_id": pool_id} for suite in suites],
                query={
                    "async_mode": "true",
                    "operation_id": operation_id,
                    "skip_invalid_items": "false",
                    "open_pool": "false",
                },
            )
        self.api.wait(operation)

    def upload_training(self, training_id):
        rows = self.plan["examples"]["practice"]
        tasks = [
            {
                "pool_id": training_id,
                "input_values": {
                    k: v for k, v in row.items() if k not in {"verdict", "hint"}
                },
                "known_solutions": [
                    {
                        "output_values": {"verdict": row["verdict"]},
                        "correctness_weight": 1.0,
                    }
                ],
                "message_on_unknown_solution": row["hint"],
                "infinite_overlap": True,
            }
            for row in rows
        ]
        operation_id = self.state["operations"].setdefault(
            "training", str(uuid.uuid4())
        )
        self.save()
        try:
            operation = self.api.request("GET", f"/operations/{operation_id}")
        except ApiError as error:
            if error.status != 404:
                raise
            operation = self.api.request(
                "POST",
                "/tasks",
                tasks,
                query={
                    "async_mode": "true",
                    "operation_id": operation_id,
                    "skip_invalid_items": "false",
                    "open_pool": "false",
                },
            )
        self.api.wait(operation)

    def create(self):
        marker = "review:" + self.state["run_id"]
        self.ensure(
            "project", "/projects", project_body(marker), "private_comment", marker
        )
        training = {
            "project_id": self.state["resources"]["project"],
            "private_name": "Обучение review " + marker,
            "may_contain_adult_content": False,
            "assignment_max_duration_seconds": self.plan["config"][
                "assignment_seconds"
            ],
            "mix_tasks_in_creation_order": True,
            "shuffle_tasks_in_task_suite": False,
            "training_tasks_in_task_suite_count": 1,
            "task_suites_required_to_pass": len(self.plan["examples"]["practice"]),
            "inherited_instructions": True,
        }
        training_id = self.ensure(
            "training", "/trainings", training, "private_name", training["private_name"]
        )
        self.upload_training(training_id)
        skill = {
            "name": "Butterfly bbox review",
            "private_comment": marker + ":exam",
            "hidden": True,
            "public_name": {
                "RU": "Проверка рамок бабочек",
                "EN": "Butterfly bbox review",
            },
            "public_requester_description": {
                "RU": "Допуск к независимой проверке рамок",
                "EN": "Review qualification",
            },
        }
        self.ensure("exam_skill", "/skills", skill, "private_comment", marker + ":exam")
        access = {
            "name": "Butterfly bbox review access",
            "private_comment": marker + ":access",
            "hidden": True,
            "public_name": {
                "RU": "Доступ к проверке рамок бабочек",
                "EN": "Butterfly bbox review access",
            },
            "public_requester_description": {
                "RU": "Текущий допуск к независимой проверке",
                "EN": "Current review access",
            },
        }
        self.ensure(
            "access_skill", "/skills", access, "private_comment", marker + ":access"
        )
        batches = {
            "exam": (
                self.plan["exam"],
                self.plan["config"]["exam_price"],
                self.plan["config"]["exam_attempts"],
                False,
            ),
            "pilot": (
                self.plan["pilot"],
                self.plan["config"]["review_price"],
                self.plan["config"]["overlap"],
                True,
            ),
            "main": (
                self.plan["main"],
                self.plan["config"]["review_price"],
                self.plan["config"]["overlap"],
                True,
            ),
        }
        for key, (suites, price, overlap, qualified) in batches.items():
            title = {
                "exam": "Экзамен review",
                "pilot": "Пилот review",
                "main": "Основной review",
            }[key]
            name = title + " " + marker
            pool_id = self.ensure(
                key,
                "/pools",
                self.pool_body(name, price, overlap, qualified),
                "private_name",
                name,
            )
            self.upload(key, pool_id, suites)
        return self.state

    def score_exam(self):
        pool_id = self.state["resources"]["exam"]
        expected = {
            row["answer_id"]: row["verdict"] for row in self.plan["examples"]["exam"]
        }
        scores = self.state.setdefault("exam_scores", {})
        for assignment in self.api.items("/assignments", pool_id=pool_id):
            if assignment["status"] != "ACCEPTED":
                continue
            answers = {}
            for task, solution in zip(
                assignment["tasks"], assignment["solutions"], strict=True
            ):
                answers[task["input_values"]["answer_id"]] = solution[
                    "output_values"
                ].get("verdict")
            correct = sum(answers.get(key) == value for key, value in expected.items())
            score = 100 * correct / len(expected)
            self.api.skill(
                self.state["resources"]["exam_skill"], assignment["user_id"], score
            )
            self.api.skill(
                self.state["resources"]["access_skill"],
                assignment["user_id"],
                100 if score >= self.plan["config"]["exam_pass"] else 0,
            )
            scores[assignment["id"]] = {
                "worker_id": assignment["user_id"],
                "score": score,
            }
            self.save()
        return scores

    def score_controls(self):
        expected = {
            row["answer_id"]: row["verdict"]
            for row in self.plan["examples"]["controls"]
        }
        report = {}
        for key in ("pilot", "main"):
            pool_id = self.state["resources"][key]
            for assignment in self.api.items("/assignments", pool_id=pool_id):
                if assignment["status"] != "ACCEPTED":
                    continue
                result = report.setdefault(
                    assignment["user_id"], {"correct": 0, "total": 0}
                )
                for task, solution in zip(
                    assignment["tasks"], assignment["solutions"], strict=True
                ):
                    answer_id = task["input_values"]["answer_id"]
                    if answer_id not in expected:
                        continue
                    actual = solution["output_values"].get("verdict")
                    result["correct"] += actual == expected[answer_id]
                    result["total"] += 1
        for worker, result in report.items():
            result["score"] = 100 * result["correct"] / result["total"]
            result["passed"] = (
                result["total"] >= self.plan["config"]["control_min_answers"]
                and result["score"] >= self.plan["config"]["control_pass"]
            )
            if result["total"] >= self.plan["config"]["control_min_answers"]:
                self.api.skill(
                    self.state["resources"]["access_skill"],
                    worker,
                    100 if result["passed"] else 0,
                )
        self.state["control_scores"] = report
        self.save()
        return report

    def sync(self):
        rows = []
        for key in ("pilot", "main"):
            pool_id = self.state["resources"].get(key)
            if pool_id:
                rows.extend(self.api.items("/assignments", pool_id=pool_id))
        path = self.directory / "assignments.json"
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"assignments": len(rows), "path": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    preview_parser = subparsers.add_parser("preview")
    preview_parser.add_argument("--output", type=Path, default=ROOT / "preview.html")
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--run", type=Path, required=True)
    score_parser = subparsers.add_parser("score-exam")
    score_parser.add_argument("--run", type=Path, required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--run", type=Path, required=True)
    controls_parser = subparsers.add_parser("score-controls")
    controls_parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    plan = build()
    if args.command == "plan":
        print(
            json.dumps(
                {
                    "practice": len(plan["examples"]["practice"]),
                    "exam": len(plan["examples"]["exam"]),
                    "controls": len(plan["examples"]["controls"]),
                    "pilot_pages": len(plan["pilot"]),
                    "main_pages": len(plan["main"]),
                    "budget": plan["budget"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "preview":
        preview(public_input(read_json(DEFAULT_SOURCE)[0]), args.output)
        print(args.output)
    elif args.command == "create":
        print(json.dumps(Creator(args.run).create(), ensure_ascii=False, indent=2))
    elif args.command == "score-exam":
        print(json.dumps(Creator(args.run).score_exam(), ensure_ascii=False, indent=2))
    elif args.command == "score-controls":
        print(
            json.dumps(Creator(args.run).score_controls(), ensure_ascii=False, indent=2)
        )
    else:
        print(json.dumps(Creator(args.run).sync(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
