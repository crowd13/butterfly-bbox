"""Rebuild local draft previews without changing annotations or human reviews.

Run from the repository root: python3 -m crowd.bbox.fixtures.build_preview
"""

import html
import json
from pathlib import Path

from crowd.bbox.render import assets, preview

ROOT = Path(__file__).parent / "gold-draft"
POLICY = (
    "Размечаем всех взрослых бабочек, включая мотыльков и молей. "
    "Гусениц, куколок и кладки яиц не размечаем."
)


def main():
    bank = json.loads((ROOT / "gold_draft.json").read_text())
    view = assets({"practice": []})
    gallery = [
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Черновик эталонов</title>',
        "<style>body{font:16px system-ui;max-width:1400px;margin:30px auto;padding:16px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}"
        "svg{width:100%;height:340px;background:#eee}article{padding:12px;border:1px solid #ccc}"
        "a{margin-right:12px}</style>",
        "<h1>Черновик ИИ: 110 изображений</h1><p>Требуется независимая человеческая проверка.</p>",
        f"<p>{POLICY}</p>",
        '<p><a href="blind.html">Независимая разметка без подсказок</a>'
        '<a href="gold_draft.json">JSON</a><a href="report.json">Отчёт</a></p>',
    ]
    blind = gallery[:2] + [
        "<h1>Независимая разметка</h1>",
        f"<p>{POLICY}</p>",
        "<p>Сохраните собственные ответы до просмотра разметки ИИ или другого участника.</p>",
    ]
    for split, rows in bank.items():
        gallery.append(f'<h2>{split}: {len(rows)}</h2><div class="grid">')
        blind.append(f"<h2>{split}</h2><ol>")
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
                f'<a href="ai-review/{pid}.html">Исправить черновик</a>'
                f'<a href="{html.escape(row["observation_url"], quote=True)}">'
                "Источник</a></article>"
            )
            blind.append(f'<li><a href="preview/{pid}.html">{pid}</a></li>')
            target = ROOT / "preview" / f"{pid}.html"
            preview(
                {"assets": view, "manifest": [dict(row, image_url=f"../images/{pid}.jpg")]}, target
            )
            page = target.read_text().replace(
                "<h1>Предпросмотр bbox</h1>", f"<h1>Предпросмотр bbox</h1><p>{POLICY}</p>"
            )
            download = """<button id="download">Скачать ответ JSON</button><script>
document.getElementById('download').onclick=function(){
 var err=task.validate();if(err){alert(JSON.stringify(err));return;}
 var record={photo_id:__PID__,solution:task.getSolution().output_values};
 var url=URL.createObjectURL(new Blob([JSON.stringify(record,null,2)],{type:'application/json'}));
 var link=document.createElement('a');link.href=url;
 link.download='annotation-'+record.photo_id+'.json';link.click();
 setTimeout(function(){URL.revokeObjectURL(url);},1000);
};</script>""".replace("__PID__", json.dumps(pid))
            page = page.replace("</html>", download + "</html>")
            target.write_text(page)
            initial = json.dumps({"output_values": row["solution"]})
            page = page.replace("task.onRender();", f"task.solution = {initial}; task.onRender();")
            page = page.replace(
                "<h1>Предпросмотр bbox</h1>",
                f"<h1>Черновик ИИ — требует проверки</h1><p>{hint}</p>",
            )
            (ROOT / "ai-review" / f"{pid}.html").write_text(page)
        gallery.append("</div>")
        blind.append("</ol>")
    (ROOT / "index.html").write_text("\n".join(gallery) + "</html>")
    (ROOT / "blind.html").write_text("\n".join(blind) + "</html>")


if __name__ == "__main__":
    main()
