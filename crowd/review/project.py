"""Prepare and create the independent bbox review project."""

import argparse
import getpass
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crowd.bbox.api import ApiError, TasksApi
from crowd.review.account_guard import bind_account, verify_bound_account
from crowd.review.bundle import export_bundle
from crowd.review.exam_mode import use_exam, use_native
from crowd.review.interface_update import update_interface
from crowd.review.native_check import NativeCheck
from crowd.review.prepare import (
    budget,
    control_specs,
    fixtures,
    pages,
    read_json,
    training_tasks,
)
from crowd.review.render import preview, project_body
from crowd.review.verification import verify_resources, verify_tasks, verify_upload

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT.parent / "bbox/runs/final/review.json"
DEFAULT_GOLD = ROOT.parent / "bbox/fixtures/gold/gold.json"


def build(source=DEFAULT_SOURCE, gold=DEFAULT_GOLD):
    config = read_json(ROOT / "config.json")
    size = config["work_per_page"]
    if (
        size <= 0
        or config["overlap"] != 2
        or not 0 < config["pilot_size"] < config["dataset_size"]
        or config["pilot_size"] % size
    ):
        raise ValueError("Expected complete pilot pages and two independent reviews")
    specs = read_json(ROOT / "fixtures.json")
    bank = read_json(gold)
    examples = {name: fixtures(bank, rows) for name, rows in specs.items()}
    if {row["photo_id"] for row in examples["practice"]} & {
        row["photo_id"] for row in examples["exam"]
    }:
        raise ValueError("Practice and exam photos overlap")
    excluded = {
        row["photo_id"] for name in ("practice", "exam") for row in examples[name]
    }
    control_count = config["dataset_size"] // config["work_per_page"]
    examples["controls"] = fixtures(bank, control_specs(bank, excluded, control_count))
    answers = read_json(source)
    if {str(row["photo_id"]) for row in answers} & {
        str(row["photo_id"]) for rows in bank.values() for row in rows
    }:
        raise ValueError("Gold photos overlap with the working dataset")
    suites = pages(answers, examples["controls"], config)
    pilot_pages = config["pilot_size"] // config["work_per_page"]
    return {
        "config": config,
        "source": answers,
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
        snapshot = self.directory / "snapshot.json"
        self.state = (
            read_json(self.path)
            if self.path.exists()
            else {"run_id": str(uuid.uuid4()), "resources": {}, "operations": {}}
        )
        self.api = api or TasksApi()
        verify_bound_account(self.api, self.state)
        if snapshot.exists() and not self.path.exists():
            raise ValueError("Snapshot has no state; use a new run directory")
        if self.path.exists() and not snapshot.exists() and any(
            self.state.get(key) for key in ("resources", "operations")
        ):
            raise ValueError("Existing run has no snapshot; use a new run directory")
        if not snapshot.exists():
            self.plan = build()
            frozen = {
                "plan": self.plan,
                "project": project_body(
                    "review:" + self.state["run_id"],
                    self.plan["examples"]["practice"] if self.plan["config"].get("training_mode") == "inline" else [],
                ),
            }
            self.save()
            snapshot.write_text(
                json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        frozen = read_json(snapshot)
        self.plan = frozen["plan"]
        self.project = frozen["project"]
        source_path = self.directory / "source.json"
        if not source_path.exists():
            source_path.write_text(
                json.dumps(self.plan["source"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def ensure(self, key, path, body, marker_field, marker):
        body = self.state.setdefault("definitions", {}).setdefault(key, body)
        self.save()
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

    @staticmethod
    def golden_set(skill_id, minimum, history_size):
        return {
            "collector_config": {
                "type": "GOLDEN_SET",
                "parameters": {"history_size": history_size},
            },
            "rules": [
                {
                    "conditions": [
                        {
                            "key": "golden_set_answers_count",
                            "operator": "GTE",
                            "value": minimum,
                        }
                    ],
                    "action": {
                        "type": "SET_SKILL_FROM_OUTPUT_FIELD",
                        "parameters": {
                            "skill_id": skill_id,
                            "from_field": "correct_answers_rate",
                        },
                    },
                }
            ],
        }

    def pool_body(self, key, name, marker, price, overlap, qualified):
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
                        "operator": "GTE",
                        "value": self.plan["config"]["control_pass"],
                    },
                ]
            )
        body = {
            "project_id": self.state["resources"]["project"],
            "private_name": name,
            "private_comment": marker,
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
        skill = self.state["resources"][
            "exam_skill" if key == "exam" else "access_skill"
        ]
        minimum = (
            len(self.plan["examples"]["exam"])
            if key == "exam"
            else self.plan["config"]["control_min_answers"]
        )
        body["quality_control"] = {
            "configs": [self.golden_set(skill, minimum, minimum)]
        }
        if key == "exam":
            body["quality_control"]["configs"][0]["rules"].append(
                {
                    "conditions": [
                        {
                            "key": "golden_set_answers_count",
                            "operator": "GTE",
                            "value": minimum,
                        },
                        {
                            "key": "correct_answers_rate",
                            "operator": "GTE",
                            "value": self.plan["config"]["exam_pass"],
                        },
                    ],
                    "action": {
                        "type": "SET_SKILL",
                        "parameters": {
                            "skill_id": self.state["resources"]["access_skill"],
                            "skill_value": 100,
                        },
                    },
                }
            )
        if key == "exam":
            if self.plan["config"].get("training_mode", "native") == "native":
                body["quality_control"]["training_requirement"] = {
                    "training_pool_id": self.state["resources"]["training"],
                    "training_passing_skill_value": 0,
                }
            body["filter"]["and"].append(
                {"or": [{
                    "category": "skill",
                    "key": self.state["resources"]["exam_skill"],
                    "operator": "EQ",
                    "value": None,
                }]}
            )
        return body

    def upload(self, key, pool_id, suites):
        self.upload_items(key, pool_id, suites, "/task-suites")

    def upload_items(self, key, pool_id, items, path):
        if key in self.state.get("uploaded", []):
            verify_tasks(self.api, path, pool_id, items)
            return
        operation_id = self.state["operations"].setdefault(key, str(uuid.uuid4()))
        self.save()
        try:
            operation = self.api.request("GET", f"/operations/{operation_id}")
        except ApiError as error:
            if error.status != 404:
                raise
            operation = self.api.request(
                "POST",
                path,
                [{**item, "pool_id": pool_id} for item in items],
                query={
                    "async_mode": "true",
                    "operation_id": operation_id,
                    "skip_invalid_items": "false",
                    "open_pool": "false",
                },
            )
        completed = self.api.wait(operation)
        verify_upload(completed, len(items))
        verify_tasks(self.api, path, pool_id, items)
        self.state.setdefault("uploaded", []).append(key)
        self.save()

    def training_tasks(self):
        return training_tasks(self.plan)

    def upload_training(self, training_id):
        self.upload_items("training", training_id, self.training_tasks(), "/tasks")

    def create(self):
        marker = "review:" + self.state["run_id"]
        self.ensure(
            "project", "/projects", self.project, "private_comment", marker
        )
        training = {
            "project_id": self.state["resources"]["project"],
            "private_name": "Обучение проверки рамок ["
            + self.state["run_id"][:8]
            + "]",
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
                "exam": "Экзамен проверки рамок",
                "pilot": "Пилот проверки рамок",
                "main": "Основная проверка рамок",
            }[key]
            pool_marker = marker + ":" + key
            pool_id = self.ensure(
                key,
                "/pools",
                self.pool_body(key, title, pool_marker, price, overlap, qualified),
                "private_comment",
                pool_marker,
            )
            self.upload(key, pool_id, suites)
        return self.state

    def verify(self):
        resources = self.state["resources"]
        if set(resources) != {
            "project", "training", "exam_skill", "access_skill", "exam", "pilot", "main"
        }:
            raise ValueError("Run is incomplete; rerun create before verify")
        statuses = verify_resources(self.api, self.state, self.directory / "verification.json")
        verify_tasks(self.api, "/tasks", resources["training"], self.training_tasks())
        for key in ("exam", "pilot", "main"):
            verify_tasks(self.api, "/task-suites", resources[key], self.plan[key])
        return {
            "resources": resources,
            "verified": True,
            "statuses": statuses,
            "training": len(self.plan["examples"]["practice"]),
            "exam_pages": len(self.plan["exam"]),
            "pilot_pages": len(self.plan["pilot"]),
            "main_pages": len(self.plan["main"]),
        }

    def score_exam(self, apply=False):
        if apply and any(
            True
            for key in ("pilot", "main")
            for _ in self.api.items("/assignments", pool_id=self.state["resources"][key])
        ):
            raise ValueError("Exam recovery is only allowed before any paid review assignments")
        pool_id = self.state["resources"]["exam"]
        expected = {
            row["answer_id"]: row["verdict"] for row in self.plan["examples"]["exam"]
        }
        scores = {}
        applied = self.state.setdefault("exam_scores_applied", [])
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
            if apply and assignment["id"] not in applied:
                self.api.skill(
                    self.state["resources"]["exam_skill"], assignment["user_id"], score
                )
                self.api.skill(
                    self.state["resources"]["access_skill"],
                    assignment["user_id"],
                    100 if score >= self.plan["config"]["exam_pass"] else 0,
                )
                applied.append(assignment["id"])
            scores[assignment["id"]] = {
                "worker_id": assignment["user_id"],
                "score": score,
            }
        self.state["exam_scores"] = scores
        self.save()
        return scores

    def update_interface(self):
        mode = self.state.get("training_mode", self.plan["config"].get("training_mode", "native"))
        practice = self.plan["examples"]["practice"] if mode == "inline" else []
        view = project_body("review:" + self.state["run_id"], practice)["task_spec"]["view_spec"]
        result = update_interface(self.api, self.state, self.directory, view)
        self.project = self.state["definitions"]["project"]
        return {**result, "verification": self.verify()}

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
                controls = 0
                for task, solution in zip(
                    assignment["tasks"], assignment["solutions"], strict=True
                ):
                    answer_id = task["input_values"]["answer_id"]
                    if answer_id not in expected:
                        continue
                    actual = solution["output_values"].get("verdict")
                    result["correct"] += actual == expected[answer_id]
                    result["total"] += 1
                    controls += 1
                if controls != 1:
                    raise ValueError(f"Expected one control in assignment {assignment['id']}")
        for result in report.values():
            result["score"] = 100 * result["correct"] / result["total"]
            result["passed"] = (
                result["total"] >= self.plan["config"]["control_min_answers"]
                and result["score"] >= self.plan["config"]["control_pass"]
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
    create_parser.add_argument("--prompt-token", action="store_true")
    create_parser.add_argument("--source-project")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--run", type=Path, required=True)
    verify_parser.add_argument("--prompt-token", action="store_true")
    update_parser = subparsers.add_parser("update-interface")
    update_parser.add_argument("--run", type=Path, required=True)
    update_parser.add_argument("--prompt-token", action="store_true")
    for command in ("use-exam", "use-native"):
        mode_parser = subparsers.add_parser(command)
        mode_parser.add_argument("--run", type=Path, required=True)
        mode_parser.add_argument("--prompt-token", action="store_true")
    native_parser = subparsers.add_parser("native-check")
    native_parser.add_argument("--run", type=Path, required=True)
    native_parser.add_argument("--prompt-token", action="store_true")
    native_actions = native_parser.add_mutually_exclusive_group()
    native_actions.add_argument("--open", action="store_true")
    native_actions.add_argument("--close", action="store_true")
    native_parser.add_argument("--watch-seconds", type=int, default=0)
    bundle_parser = subparsers.add_parser("bundle")
    bundle_parser.add_argument("--output", type=Path, required=True)
    score_parser = subparsers.add_parser("score-exam")
    score_parser.add_argument("--run", type=Path, required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--run", type=Path, required=True)
    controls_parser = subparsers.add_parser("score-controls")
    controls_parser.add_argument("--run", type=Path, required=True)
    score_parser.add_argument("--apply", action="store_true")
    for remote_parser in (score_parser, controls_parser, sync_parser):
        remote_parser.add_argument("--prompt-token", action="store_true")
    args = parser.parse_args()
    if args.command == "native-check" and (args.watch_seconds < 0 or (
        args.open and args.watch_seconds == 0
    )):
        parser.error("Use --watch-seconds greater than zero with --open")
    if hasattr(args, "run") and args.command != "create" and not (args.run / "state.json").exists():
        parser.error("Run does not exist; create it first")
    api = None
    if getattr(args, "prompt_token", False):
        api = TasksApi(getpass.getpass("OAuth: "))
    if args.command == "plan":
        plan = build()
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
        preview(
            [
                {key: value for key, value in row.items() if key not in {"verdict", "hint"}}
                for row in build()["examples"]["practice"]
            ],
            args.output,
        )
        print(args.output)
    elif args.command == "create":
        creator = Creator(args.run, api)
        account = None
        if args.source_project is not None:
            account = bind_account(
                creator.api, creator.state, creator.directory, args.source_project,
                DEFAULT_SOURCE.with_name("assignments.json"),
            )
        creator.create()
        result = creator.verify()
        if account is not None:
            result["account"] = account
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "verify":
        print(json.dumps(Creator(args.run, api).verify(), ensure_ascii=False, indent=2))
    elif args.command == "update-interface":
        print(json.dumps(Creator(args.run, api).update_interface(), ensure_ascii=False, indent=2))
    elif args.command in {"use-exam", "use-native"}:
        creator = Creator(args.run, api)
        stopped = None
        if (args.run / "native-check-state.json").exists():
            stopped = NativeCheck(creator).stop()
        migrate = use_native if args.command == "use-native" else use_exam
        result = migrate(creator.api, creator.state, creator.directory)
        result["interface"] = creator.update_interface()
        if stopped is not None:
            result["native_check"] = {key: stopped[key] for key in ("pool_id", "pool_status", "accepted")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "native-check":
        if args.close and not (args.run / "native-check-state.json").exists():
            parser.error("No native check exists for this run")
        check = NativeCheck(Creator(args.run, api))
        result = check.stop() if args.close else check.run(args.open, args.watch_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "bundle":
        plan = build()
        practice = plan["examples"]["practice"] if plan["config"].get("training_mode") == "inline" else []
        print(export_bundle(plan, args.output, project_body("review:manual", practice)))
    elif args.command == "score-exam":
        print(
            json.dumps(Creator(args.run, api).score_exam(args.apply), ensure_ascii=False, indent=2)
        )
    elif args.command == "score-controls":
        print(
            json.dumps(Creator(args.run, api).score_controls(), ensure_ascii=False, indent=2)
        )
    else:
        print(json.dumps(Creator(args.run, api).sync(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ApiError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)
        raise SystemExit(130) from None
