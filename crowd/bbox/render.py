"""Build the public interface; only practice answers are embedded."""

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def public_image(row):
    return {
        "photo_id": str(row["photo_id"]),
        "image_url": row["image_url"],
        "width": int(row["width"]),
        "height": int(row["height"]),
    }


def assets(bank, iou_threshold=0.85):
    practice = [
        {**public_image(r), "solution": r["solution"], "hint": r["hint"]} for r in bank["practice"]
    ]
    script = (
        (ROOT / "interface/editor.js").read_text()
        + "\n"
        + (ROOT / "interface/training.js")
        .read_text()
        .replace("__TRAINING_IOU__", str(iou_threshold))
        + "\n"
        + (ROOT / "interface/task.js")
        .read_text()
        .replace(
            "__PRACTICE_JSON__", json.dumps(practice, ensure_ascii=False).replace("<", "\\u003c")
        )
    )
    instructions = (ROOT / "instruction.html").read_text()
    examples = []
    for r in practice:
        boxes = "".join(
            f'<rect x="{b["left"] * r["width"]}" y="{b["top"] * r["height"]}" '
            f'width="{b["width"] * r["width"]}" height="{b["height"] * r["height"]}" '
            'fill="none" stroke="red" stroke-width="3"/>'
            for b in r["solution"]["boxes"]
        )
        examples.append(
            f'<figure><svg viewBox="0 0 {r["width"]} {r["height"]}" width="400" '
            f'height="{400 * r["height"] / r["width"]}">'
            f'<image href="{html.escape(r["image_url"], quote=True)}" width="{r["width"]}" '
            f'height="{r["height"]}"/>{boxes}</svg>'
            f"<figcaption>{html.escape(r['hint'])}</figcaption></figure>"
        )
    return {
        "script": script,
        "markup": (ROOT / "interface/task.html").read_text(),
        "styles": (ROOT / "interface/task.css").read_text(),
        "instructions": instructions.replace("<!-- PRACTICE_EXAMPLES -->", "\n".join(examples)),
    }


def project_body(snapshot, marker):
    view = snapshot["assets"]
    return {
        "public_name": "Найдите бабочек на фотографиях",
        "public_description": (
            "Выделите всех взрослых бабочек, включая мотыльков и молей, или отметьте их отсутствие."
        ),
        "public_instructions": view["instructions"],
        "private_comment": marker,
        "assignments_issuing_type": "AUTOMATED",
        "assignments_automerge_enabled": False,
        "task_spec": {
            "input_spec": {
                "photo_id": {"type": "string", "required": True, "hidden": True},
                "image_url": {"type": "url", "required": True},
                "width": {"type": "integer", "required": True, "hidden": True},
                "height": {"type": "integer", "required": True, "hidden": True},
            },
            "output_spec": {
                "status": {
                    "type": "string",
                    "required": True,
                    "allowed_values": ["boxes", "no_butterflies", "uncertain", "load_error"],
                },
                "boxes": {"type": "array_json", "required": True},
            },
            "view_spec": {
                "markup": view["markup"],
                "script": view["script"],
                "styles": view["styles"],
                "settings": {
                    "showSkip": True,
                    "showTimer": True,
                    "showTitle": True,
                    "showSubmit": True,
                    "showFullscreen": True,
                    "showInstructions": True,
                    "showFinish": True,
                    "showMessage": True,
                    "showReward": True,
                },
            },
        },
    }


def preview(snapshot, path):
    """Exercise the same adapter against a small Tasks API mock, without credentials."""
    assets_ = snapshot["assets"]
    sample = public_image(snapshot["manifest"][0])
    mock = """var exports = {}; var previewInput = __INPUT__;
function TolokaTask(options) { this.element = options.element; this.solution = {output_values:{}}; }
TolokaTask.prototype.getDOMElement = function(){return this.element;};
TolokaTask.prototype.getTask = function(){return {id:'preview',input_values:previewInput};};
TolokaTask.prototype.getSolution = function(){return this.solution;};
TolokaTask.prototype.setSolutionOutputValues = function(value){this.solution.output_values=value;};
TolokaTask.prototype.validate = function(){return null;};
""".replace("__INPUT__", json.dumps(sample).replace("<", "\\u003c"))
    page = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Bbox preview</title>'
    page += f'<style>{assets_["styles"]}</style><h1>Предпросмотр bbox</h1><div id="task">'
    page += (
        assets_["markup"]
        + '</div><button id="submit">Проверить ответ</button><pre id="output"></pre>'
    )
    code = (
        mock
        + assets_["script"]
        + """
var task = new exports.Task({element:document.getElementById('task')}); task.onRender();
document.getElementById('submit').onclick=function(){document.getElementById('output').textContent =
 JSON.stringify(task.validate() || task.getSolution(),null,2);};
"""
    )
    page += "<script>" + code.replace("</script", "<\\/script") + "</script></html>"
    Path(path).write_text(page, encoding="utf-8")
