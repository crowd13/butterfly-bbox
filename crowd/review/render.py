"""Render the review project and its local preview."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def assets():
    return {
        "markup": (ROOT / "interface/task.html").read_text(encoding="utf-8"),
        "styles": (ROOT / "interface/task.css").read_text(encoding="utf-8"),
        "script": (ROOT / "interface/task.js").read_text(encoding="utf-8"),
        "instructions": (ROOT / "instruction.html").read_text(encoding="utf-8"),
    }


def project_body(marker):
    view = assets()
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
    mock = """var exports={}; function TolokaTask(o){this.element=o.element;this.solution={output_values:{}}}
TolokaTask.prototype.getDOMElement=function(){return this.element};
TolokaTask.prototype.getTask=function(){return {id:'preview',input_values:__INPUT__}};
TolokaTask.prototype.getSolution=function(){return this.solution};
TolokaTask.prototype.setSolutionOutputValues=function(v){this.solution.output_values=v};
TolokaTask.prototype.validate=function(){return null};
""".replace(
        "__INPUT__",
        json.dumps(input_values, ensure_ascii=False).replace("<", "\\u003c"),
    )
    script = (
        mock
        + view["script"]
        + """
var task=new exports.Task({element:document.getElementById('task')});task.onRender();
document.getElementById('check').onclick=function(){document.getElementById('result').textContent=JSON.stringify(task.validate()||task.getSolution(),null,2)};
"""
    )
    page = "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Review preview</title>"
    page += f"<style>{view['styles']}</style><div id='task'>{view['markup']}</div>"
    page += "<button id='check'>Проверить</button><pre id='result'></pre>"
    page += "<script>" + script.replace("</script", "<\\/script") + "</script></html>"
    Path(output).write_text(page, encoding="utf-8")
