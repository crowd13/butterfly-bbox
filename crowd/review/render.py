"""Render the review project and its local preview."""

import json
from pathlib import Path

from crowd.review.prepare import fixtures, read_json

ROOT = Path(__file__).resolve().parent


def practice_examples():
    bank = read_json(ROOT.parent / "bbox/fixtures/gold/gold.json")
    return fixtures(bank, read_json(ROOT / "fixtures.json")["practice"])


def assets(practice=None):
    practice = practice_examples() if practice is None else practice
    examples = [
        {key: row[key] for key in ("image_url", "source_boxes", "verdict", "hint")}
        for row in practice
    ]
    data = json.dumps(examples, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return {
        "markup": (ROOT / "interface/task.html").read_text(encoding="utf-8"),
        "styles": (ROOT / "interface/task.css").read_text(encoding="utf-8"),
        "script": "var reviewPractice = " + data + ";\n" + (ROOT / "interface/task.js").read_text(encoding="utf-8"),
        "instructions": (ROOT / "instruction.html").read_text(encoding="utf-8"),
    }


def project_body(marker, practice=None):
    view = assets(practice)
    return {
        "public_name": "Проверьте рамки бабочек",
        "public_description": "Проверьте, все ли взрослые бабочки найдены и точно выделены.",
        "public_instructions": view["instructions"],
        "private_comment": marker,
        "assignments_issuing_type": "AUTOMATED",
        "assignments_automerge_enabled": False,
        "task_spec": {
            "input_spec": {
                "answer_id": {"type": "string", "required": True, "hidden": False},
                "photo_id": {"type": "string", "required": True, "hidden": False},
                "image_url": {"type": "url", "required": True},
                "width": {"type": "integer", "required": True, "hidden": False},
                "height": {"type": "integer", "required": True, "hidden": False},
                "source_status": {"type": "string", "required": True, "hidden": False},
                "source_boxes": {
                    "type": "array_json",
                    "required": True,
                    "hidden": False,
                },
            },
            "output_spec": {
                "verdict": {
                    "type": "string",
                    "required": True,
                    "allowed_values": [
                        "OK",
                        "MISSING",
                        "EXTRA",
                        "BOUNDARY",
                        "LOAD_ERROR",
                    ],
                }
            },
            "view_spec": {
                "markup": view["markup"],
                "styles": view["styles"],
                "script": view["script"],
                "settings": {"showSkip": True, "showTimer": True, "showSubmit": True},
            },
        },
    }


def preview(input_values, output):
    view = assets()
    inputs = [input_values] if isinstance(input_values, dict) else list(input_values)
    fields = project_body("preview")["task_spec"]["input_spec"]
    tasks = [
        {
            "id": f"preview-task-{index}",
            "input_values": {key: row[key] for key in fields if key in row},
        }
        for index, row in enumerate(inputs, start=1)
    ]
    mock = """var exports={};
function TolokaAssignment(options){this.options=options;}
TolokaAssignment.prototype.getId=function(){return undefined;};
TolokaAssignment.prototype.onValidationDone=function(){};
function TolokaTask(o){
  this.element=o.element;this.task=o.task;this.solution={output_values:{}};
}
TolokaTask.prototype.getDOMElement=function(){return this.element};
TolokaTask.prototype.getTask=function(){return this.task};
TolokaTask.prototype.getSolution=function(){return this.solution};
TolokaTask.prototype.setSolutionOutputValues=function(v){this.solution.output_values=v};
TolokaTask.prototype.validate=function(){return null};
"""
    script = (
        mock
        + view["script"]
        + "\nvar previewTasks = "
        + json.dumps(tasks, ensure_ascii=False).replace("<", "\\u003c")
        + ";\n"
        + """
var tasks=previewTasks.map(function(data){
  var task=new exports.Task({element:document.getElementById(data.id),task:data});
  task.onRender();
  return task;
});
document.getElementById('check').onclick=function(){
  var results=tasks.map(function(task){
    return task.validate()||{
      task_id:task.getTask().id,output_values:task.getSolution().output_values
    };
  });
  document.getElementById('result').textContent=JSON.stringify(results,null,2);
};
"""
    )
    cards = "".join(
        f"<article id='{task['id']}'><h2>Задание {index}</h2>{view['markup']}</article>"
        for index, task in enumerate(tasks, start=1)
    )
    safe_script = script.replace("</script", "<\\/script")
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Проверка рамок — предпросмотр</title>
  <style>
{view['styles']}
main {{max-width: 940px; margin: auto; padding: 16px;}}
article {{margin: 32px 0; padding-bottom: 24px; border-bottom: 1px solid #bbb;}}
pre {{white-space: pre-wrap;}}
  </style>
</head>
<body><main>
  <h1>Проверка рамок — локальный предпросмотр</h1>
  <p>Выберите ответ для каждой фотографии и нажмите «Проверить все ответы».
  Здесь проверяется заполнение; серверная проверка обучения доступна в Яндекс Заданиях.</p>
  <details><summary>Инструкция</summary>{view['instructions']}</details>
  {cards}
  <button id="check" type="button">Проверить все ответы</button>
  <pre id="result" aria-live="polite"></pre>
</main><script>{safe_script}</script></body>
</html>"""
    Path(output).write_text(page, encoding="utf-8")
