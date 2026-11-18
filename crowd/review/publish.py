import argparse
import copy
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from crowd.review.audit import audit
from crowd.review.export import export
from crowd.review.interface_update import write_json


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def qa_flags(run, source):
    by_id = {row["answer_id"]: row for row in source}
    by_photo = {str(row["photo_id"]): row for row in source}
    result, inputs = [], {}
    for path in (run.parent / "known-qa-cases.json", run / "pilot-qa.json"):
        if not path.exists():
            continue
        name = os.path.relpath(path, run)
        raw = path.read_bytes()
        inputs[name] = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        for case in document.get("cases", []):
            row = (
                by_id.get(case["answer_id"]) if "answer_id" in case
                else by_photo.get(str(case.get("photo_id")))
            )
            if row is None or str(case.get("photo_id")) != str(row["photo_id"]):
                raise ValueError("QA flag does not match the frozen source")
            if "source_solution" in case and case["source_solution"] != row["solution"]:
                raise ValueError(f"QA flag targets a different annotation: {row['photo_id']}")
            result.append({
                **copy.deepcopy(case), "answer_id": row["answer_id"],
                "origin_file": name, "is_independent_human_review": False,
            })
    return result, inputs


def expenses(assignments):
    total = Decimal("0")
    count = 0
    for row in assignments:
        if row["status"] != "ACCEPTED":
            continue
        if "reward" not in row:
            raise ValueError(f"Accepted assignment has no actual reward: {row['id']}")
        try:
            reward = Decimal(str(row["reward"]))
        except InvalidOperation as error:
            raise ValueError(f"Invalid actual reward: {row['id']}") from error
        if not reward.is_finite() or reward < 0:
            raise ValueError(f"Invalid actual reward: {row['id']}")
        total += reward
        count += 1
    return {
        "scope": "review_work_assignments_only", "accepted_assignments": count,
        "actual_assignment_rewards_rub": format(total.quantize(Decimal("0.01")), "f"),
        "commission_assumption": "0.40", "commission_is_verified_actual": False,
        "actual_commission_rub": None, "actual_total_with_commission_rub": None,
        "estimated_total_with_commission_rub": format(
            (total * Decimal("1.40")).quantize(Decimal("0.01")), "f"
        ),
        "exam_included": False, "bonuses_included": False,
    }


def bundle_readme(manifest, output):
    origin, costs = manifest["origin"], manifest["expenses"]
    review_directory = Path(__file__).resolve().parent
    review_readme = os.path.relpath(review_directory / "README.md", output)
    coverage = os.path.relpath(
        review_directory.parent / "bbox/runs/final/coverage.json", output
    )
    commission_percent = format(Decimal(costs["commission_assumption"]) * 100, "g")
    return f"""# Выгрузка независимого review

Источник: проект bbox `{origin['source_project_id']}`; review: `{origin['review_project_id']}`.
Фотографий: {manifest['source_photos']}; с завершённым независимым review:
{manifest['reviewed_photos']}; в очереди: {manifest['pending_photos']}.

`review_collection_complete={json.dumps(manifest['review_collection_complete'])}`
означает только полноту сбора проверок. Неполное покрытие не является golden set;
полное покрытие также требует финализации человеком.
`is_golden_set={json.dumps(manifest['is_golden_set'])}`,
`golden_ready={json.dumps(manifest['golden_ready'])}`.

| Файл | Контракт |
| --- | --- |
| `source.json` | Все исходные ответы с неизменёнными рамками. |
| `reviewed-source.json` | Исходные ответы с двумя независимыми проверками. |
| `assignments.json` | Исходные назначения связанного аккаунта, включая их статусы и ответы. |
| `reviews.json` | Сырые голоса и crowd-решения для завершённых проверок. |
| `photos.jsonl` | Все фотографии, источник, голоса, raw/effective решения и ссылки на QA. |
| `pending.json` | Очередь отсутствующих проверок и ручного разбора с причинами. |
| `qa-flags.json` | Подозрения и предложения AI с происхождением; требуют проверки человеком. |
| `report.json` | Покрытие, независимость, наблюдения по контролям и расходы. |
| `manifest.json` | Происхождение, счётчики и SHA-256 входов и файлов пакета. |

Сырые crowd-решения: `{json.dumps(manifest['crowd_decisions'], sort_keys=True)}`.
Эффективные решения: `{json.dumps(manifest['effective_decisions'], sort_keys=True)}`.
AI-флаг может направить ответ в ручной разбор, сохраняя исходные голоса и рамки.
Предложение AI не является независимой человеческой проверкой или исправлением.
`ACCEPT` — кандидат для финализации, а не подтверждение готового эталона.

Контроли в `report.json` — наблюдения по исполнителям: малое число ответов
помечается `insufficient_data`, а не провалом допуска. Фактические награды за
{costs['accepted_assignments']} принятых рабочих назначений:
{costs['actual_assignment_rewards_rub']} ₽. Комиссия {commission_percent}% —
непроверенное допущение; оценка суммы с ней:
{costs['estimated_total_with_commission_rub']} ₽. Фактическая комиссия и полный
расход неизвестны; экзамен и бонусы в эту сумму не включены.

Порядок работы и воспроизведения: [review README]({review_readme}).
Полнота исходной bbox-разметки: [coverage.json]({coverage}).
"""


def export_review_bundle(run, output):
    run, output = Path(run).resolve(), Path(output).resolve()
    if run == output or run.is_relative_to(output):
        raise ValueError("Output cannot replace the source run or its parent")
    if output.exists() and not output.is_dir():
        raise ValueError("Output must be a directory")
    error_path = output.with_name(output.name + "-error.json")
    try:
        inputs = {name: run / name for name in (
            "state.json", "snapshot.json", "source.json", "assignments.json"
        )}
        raw_inputs = {name: path.read_bytes() for name, path in inputs.items()}
        loaded = {name: json.loads(value) for name, value in raw_inputs.items()}
        state, snapshot = loaded["state.json"], loaded["snapshot.json"]
        source, assignments = loaded["source.json"], loaded["assignments.json"]
        plan, resources = snapshot["plan"], state["resources"]
        if source != plan["source"] or len(source) != 400:
            raise ValueError("Expected the unchanged frozen source of all 400 photographs")
        for field in ("requester_id", "source_project_id"):
            if not isinstance(state.get(field), str) or not state[field].strip():
                raise ValueError(f"Bound export requires a nonempty {field}")
        requester_id = state.get("requester_id")
        if any(
            not isinstance(row.get("owner"), dict) or row["owner"].get("id") != requester_id
            for row in assignments
        ):
            raise ValueError("Assignment owner differs from the run's bound requester")
        report = audit(assignments, source, plan, resources)
        errors = list(report["errors"])
        for pool in report["pools"].values():
            errors.extend(pool["errors"])
            for row in pool["photos"]:
                votes = row["reviews"]
                if len(votes) >= 2 and (
                    len(votes) != 2 or len({vote["worker_id"] for vote in votes}) != 2
                ):
                    errors.append({
                        "answer_id": row["answer_id"], "message": "Invalid independence"
                    })
        if errors:
            raise ValueError("Review validation failed: " + json.dumps(errors, ensure_ascii=False))
        cases = {
            row["answer_id"]: row for pool in report["pools"].values() for row in pool["photos"]
        }
        completed = {key for key, row in cases.items() if row["decision"] is not None}
        reviewed_source = [row for row in source if row["answer_id"] in completed]
        selected = []
        for assignment in assignments:
            if assignment["status"] != "ACCEPTED":
                continue
            pairs = [(task, solution) for task, solution in zip(
                assignment["tasks"], assignment["solutions"], strict=True
            ) if task["input_values"]["answer_id"] in completed]
            if pairs:
                selected.append({
                    **assignment, "tasks": [pair[0] for pair in pairs],
                    "solutions": [pair[1] for pair in pairs],
                })
        reviews = export(selected, reviewed_source)
        flags, qa_inputs = qa_flags(run, source)
        by_flag = defaultdict(list)
        for index, flag in enumerate(flags):
            by_flag[flag["answer_id"]].append(index)
        pending, photos = [], []
        for row in source:
            case = cases[row["answer_id"]]
            reasons = []
            if case["decision"] is None:
                reasons.append("MISSING_INDEPENDENT_REVIEWS")
            elif case["decision"] == "MANUAL":
                reasons.append("CROWD_REVIEW_MANUAL")
            if row["solution"]["status"] == "uncertain":
                reasons.append("SOURCE_UNCERTAIN")
            if row["answer_id"] in by_flag:
                reasons.append("AI_QA_FLAG_REQUIRES_HUMAN_REVIEW")
            effective = (
                "PENDING_REVIEW" if case["decision"] is None else
                "MANUAL" if reasons else "ACCEPT"
            )
            photos.append({
                "answer_id": row["answer_id"], "photo_id": row["photo_id"], "source": row,
                "reviews": case["reviews"], "crowd_decision": case["decision"],
                "effective_decision": effective, "qa_refs": by_flag[row["answer_id"]],
                "pending_reasons": reasons,
            })
            if reasons:
                pending.append({
                    "answer_id": row["answer_id"], "photo_id": row["photo_id"],
                    "crowd_decision": case["decision"], "review_count": len(case["reviews"]),
                    "reasons": reasons, "requires_human_review": True,
                    "qa_flag_count": len(by_flag[row["answer_id"]]),
                })
        report["expenses"] = expenses(assignments)
        report["ready_scope"] = "review_collection_only"
        report["is_golden_set"] = False
        report["golden_ready"] = False
        values = {
            "source.json": source, "reviewed-source.json": reviewed_source,
            "reviews.json": reviews, "pending.json": pending, "assignments.json": assignments,
            "report.json": report, "qa-flags.json": flags,
            "photos.jsonl": photos,
        }
        manifest = {
            "schema_version": 1, "is_golden_set": False, "golden_ready": False,
            "human_finalization_required": True,
            "review_collection_complete": report["ready"], "source_photos": len(source),
            "reviewed_photos": len(reviewed_source), "pending_photos": len(pending),
            "crowd_decisions": dict(Counter(row["decision"] for row in reviews)),
            "effective_decisions": dict(Counter(row["effective_decision"] for row in photos)),
            "expenses": report["expenses"],
            "origin": {
                "run_id": state.get("run_id"), "requester_id": requester_id,
                "source_project_id": state.get("source_project_id"),
                "review_project_id": resources["project"],
                "resources": {key: resources[key] for key in ("training", "exam", "pilot", "main")},
                "source_pool_ids": sorted({row["pool_id"] for row in source}),
            },
            "input_sha256": {
                name: hashlib.sha256(value).hexdigest() for name, value in raw_inputs.items()
            },
            "qa_input_sha256": qa_inputs,
        }
        values["README.md"] = bundle_readme(manifest, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="." + output.name + "-", dir=output.parent) as temp:
            staging, backup = Path(temp) / "bundle", Path(temp) / "previous"
            staging.mkdir()
            for name, value in values.items():
                if name.endswith(".md"):
                    rendered = value
                elif name.endswith(".jsonl"):
                    rendered = "\n".join(json.dumps(
                        row, ensure_ascii=False, sort_keys=True, allow_nan=False
                    ) for row in value) + "\n"
                else:
                    rendered = json.dumps(
                        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
                    ) + "\n"
                (staging / name).write_text(rendered, encoding="utf-8")
            manifest["files_sha256"] = {name: checksum(staging / name) for name in values}
            (staging / "manifest.json").write_text(json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ) + "\n", encoding="utf-8")
            if output.exists():
                output.rename(backup)
            try:
                staging.rename(output)
            except OSError:
                if backup.exists():
                    backup.rename(output)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        error_path.unlink(missing_ok=True)
        return {"output": str(output), **manifest}
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as error:
        write_json(error_path, {
            "status": "EXPORT_FAILED", "is_golden_set": False, "error": str(error)
        })
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_review_bundle(args.run, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
