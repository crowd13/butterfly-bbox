"""Build the public interface; only practice answers are embedded."""

import html
import json
from html.parser import HTMLParser
from pathlib import Path

from .storage import digest, read_json

ROOT = Path(__file__).resolve().parent


def instruction_structure(source):
    """Ignore platform HTML formatting and its added link safety attributes."""

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tokens = []

        def handle_starttag(self, tag, attrs):
            attrs = [
                (key, value)
                for key, value in attrs
                if not (
                    tag == "a"
                    and key == "rel"
                    and set((value or "").split()) == {"nofollow", "noopener", "noreferrer"}
                )
            ]
            self.tokens.append(("start", tag, sorted(attrs)))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag not in {"img", "br", "hr"}:
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            self.tokens.append(("end", tag))

        def handle_data(self, data):
            if data.strip():
                self.tokens.append(("text", data))

    parser = Parser()
    parser.feed(source)
    parser.close()
    return parser.tokens


def public_image(row):
    return {
        "photo_id": str(row["photo_id"]),
        "image_url": row["image_url"],
        "width": int(row["width"]),
        "height": int(row["height"]),
    }


def assets(bank, iou_threshold=0.75):
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
    published = read_json(ROOT / "instruction-examples/manifest.json")
    by_id = {row["photo_id"]: row for row in practice}
    for example in published["examples"]:
        row = by_id.get(example["photo_id"])
        if row is None:
            continue
        if example["source_url"] != row["image_url"] or example["solution_hash"] != digest(
            row["solution"]
        ):
            raise ValueError("Instruction image is stale; rebuild and publish training examples")
        url = html.escape(published["base_url"] + example["filename"], quote=True)
        title = html.escape(example["title"])
        examples.append(
            f'<h4>{title}</h4><p><a href="{url}" target="_blank">'
            f'<img src="{url}" width="480" alt="{title}"></a></p>'
            f"<p>{html.escape(example['caption'])}</p>"
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
                "photo_id": {"type": "string", "required": True, "hidden": False},
                "image_url": {"type": "url", "required": True},
                "width": {"type": "integer", "required": True, "hidden": False},
                "height": {"type": "integer", "required": True, "hidden": False},
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
