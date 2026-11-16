"""Standalone, read-only viewer for exported annotation answers."""

import json
from pathlib import Path


def write_results(source, output):
    template = (Path(__file__).parent / "interface/results.html").read_text(encoding="utf-8")
    rows = json.loads(Path(source).read_text(encoding="utf-8"))
    data = json.dumps(rows, ensure_ascii=False, allow_nan=False)
    # Keep arbitrary answer strings from terminating the embedded JSON script.
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    Path(output).write_text(template.replace("__RESULT_DATA__", data), encoding="utf-8")
