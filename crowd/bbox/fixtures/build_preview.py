"""Rebuild the draft gallery without changing annotations or human reviews.

Run from the repository root: python3 -m crowd.bbox.fixtures.build_preview
"""

import html
import json
from pathlib import Path

ROOT = Path(__file__).parent / "gold-draft"
POLICY = (
    "Размечаем всех взрослых бабочек, включая мотыльков и молей. "
    "Гусениц, куколок и кладки яиц не размечаем."
)


def main():
    bank = json.loads((ROOT / "gold_draft.json").read_text())
    gallery = [
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Черновик эталонов</title>',
        "<style>body{font:16px system-ui;max-width:1400px;margin:30px auto;padding:16px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}"
        "svg{width:100%;height:340px;background:#eee}article{padding:12px;border:1px solid #ccc}"
        "a{margin-right:12px}</style>",
        "<h1>Черновик ИИ: 110 изображений</h1><p>Требуется проверка командой.</p>",
        f"<p>{POLICY}</p>",
        '<p><a href="gold_draft.json">JSON</a><a href="report.json">Отчёт</a></p>',
    ]
    for split, rows in bank.items():
        gallery.append(f'<h2>{split}: {len(rows)}</h2><div class="grid">')
        for row in rows:
            pid = row["photo_id"]
            width, height = int(row["width"]), int(row["height"])
            hint = html.escape(row["hint"])
            rectangles = "".join(
                f'<rect x="{b["left"] * width}" y="{b["top"] * height}" '
                f'width="{b["width"] * width}" height="{b["height"] * height}" '
                f'fill="none" stroke="#ff1744" stroke-width="{max(width, height) / 300}"/>'
                for b in row["solution"]["boxes"]
            )
            gallery.append(
                f'<article><h3>{pid}</h3><svg viewBox="0 0 {width} {height}" '
                f'role="img" aria-label="Черновик рамок {pid}">'
                f'<image href="images/{pid}.jpg" width="{width}" height="{height}"/>'
                f"{rectangles}</svg><p>{hint}</p>"
                f"<p>{html.escape(', '.join(row['scenarios']))} · "
                f"рамок: {len(row['solution']['boxes'])}</p>"
                f'<a href="{html.escape(row["observation_url"], quote=True)}">'
                "Источник</a></article>"
            )
        gallery.append("</div>")
    (ROOT / "index.html").write_text("\n".join(gallery) + "</html>")


if __name__ == "__main__":
    main()
