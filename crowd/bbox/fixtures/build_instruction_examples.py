"""Render exact approved training boxes into JPEGs for the platform instructions."""

import json
from pathlib import Path

from crowd.bbox.storage import digest, read_json, write_json
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).parents[1]
EXAMPLES = [
    (
        "761310164",
        "Одна бабочка",
        "Одна рамка плотно охватывает видимые крылья, тело, лапки и усики.",
    ),
    (
        "760354796",
        "Несколько бабочек",
        "Каждую бабочку выделяем отдельно, даже если рамки пересекаются.",
    ),
    (
        "760853346",
        "Мелкий объект",
        "Увеличьте фото и обведите бабочку, не включая окружающую траву.",
    ),
    (
        "762240560",
        "Объект обрезан краем кадра",
        "Доведите рамку до края фото; скрытую часть крыла не достраивайте.",
    ),
    (
        "762688955",
        "Взрослая моль",
        "Взрослую моль тоже размечаем: рамка включает её видимые части, но не тень.",
    ),
    ("761904711", "Похожее насекомое", "Это не бабочка: рамок нет, выбираем «Бабочек нет»."),
]


def main():
    source = ROOT / "fixtures/gold"
    bank = read_json(source / "gold.json")
    practice = {str(row["photo_id"]): row for row in bank["practice"]}
    target = ROOT / "instruction-examples"
    target.mkdir(exist_ok=True)
    records = []
    for pid, title, caption in EXAMPLES:
        row = practice[pid]
        if (row.get("team_approval") or {}).get("approved") is not True:
            raise ValueError(f"Training image {pid} is not approved")
        with Image.open(source / "images" / f"{pid}.jpg") as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            if image.size != (int(row["width"]), int(row["height"])):
                raise ValueError(f"Image size changed: {pid}")
            image.thumbnail((1200, 1200), getattr(Image, "Resampling", Image).LANCZOS)
            width, height = image.size
            draw = ImageDraw.Draw(image)
            for box in row["solution"]["boxes"]:
                coords = (
                    round(box["left"] * (width - 1)),
                    round(box["top"] * (height - 1)),
                    round((box["left"] + box["width"]) * (width - 1)),
                    round((box["top"] + box["height"]) * (height - 1)),
                )
                draw.rectangle(coords, outline="white", width=5)
                draw.rectangle(coords, outline="#ff1744", width=3)
            filename = f"{pid}.jpg"
            image.save(target / filename, quality=95, subsampling=0)
        records.append(
            {
                "photo_id": pid,
                "source_url": row["image_url"],
                "solution_hash": digest(row["solution"]),
                "title": title,
                "caption": caption,
                "filename": filename,
            }
        )
    manifest = target / "manifest.json"
    base_url = read_json(manifest).get("base_url", "") if manifest.exists() else ""
    write_json(manifest, {"base_url": base_url, "examples": records})
    print(json.dumps({"images": len(records), "directory": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
