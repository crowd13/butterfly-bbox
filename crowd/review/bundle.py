import csv
import json
from pathlib import Path

from crowd.review.prepare import training_tasks


def export_bundle(plan, directory, project):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    def write_json(name, value):
        (directory / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    write_json("project.json", project)
    write_json("source.json", plan["source"])
    view = project["task_spec"]["view_spec"]
    for name, value in {
        "interface.html": view["markup"],
        "interface.css": view["styles"],
        "interface.js": view["script"],
        "instruction.html": project["public_instructions"],
    }.items():
        (directory / name).write_text(value, encoding="utf-8")

    training = training_tasks(plan)
    write_json("training.json", training)
    for name in ("exam", "pilot", "main"):
        write_json(name + ".json", plan[name])
    write_json("settings.json", {"config": plan["config"], "budget": plan["budget"]})

    fields = list(project["task_spec"]["input_spec"])
    with (directory / "training.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["INPUT:" + key for key in fields] + ["GOLDEN:verdict", "HINT:text"])
        for task in training:
            values = []
            for key in fields:
                value = task["input_values"].get(key, "")
                field_type = project["task_spec"]["input_spec"][key]["type"]
                if field_type == "array_json":
                    value = ",".join(
                        json.dumps(item, ensure_ascii=False, allow_nan=False).replace(",", "\\,")
                        for item in value
                    )
                values.append(value)
            writer.writerow(
                values
                + [
                    task["known_solutions"][0]["output_values"]["verdict"],
                    task["message_on_unknown_solution"],
                ]
            )
    return directory
