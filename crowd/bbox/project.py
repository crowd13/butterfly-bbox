"""CLI for a single frozen bbox run. Use python -m crowd.bbox.project --help."""

import argparse
import csv
import json
import sys
from pathlib import Path

from .api import ApiError, TasksApi
from .budget import plan
from .dataset import read_manifest, select_manifest, validate_inputs
from .render import ROOT, assets, preview
from .storage import locked, read_json
from .workflow import Run, prepare


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run", type=Path, default=ROOT / "runs/default")
    sub = result.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="Select 400 images without changing the source manifest")
    select.add_argument("--manifest", type=Path, default=Path("data/manifest.csv"))
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--seed", type=int, default=42)
    for name in ("plan", "check", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=ROOT / "config.json")
        if name != "plan":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--fixtures", type=Path, required=True)
    for name in (
        "create",
        "moderate",
        "open",
        "close",
        "status",
        "export",
        "score-exam",
        "score-controls",
        "preflight",
    ):
        sub.add_parser(name)
    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("--output", type=Path, default=ROOT / "runs/preview.html")
    preview_parser.add_argument("--manifest", type=Path, help="Preview without preparing a run")
    preview_parser.add_argument("--photo-id", help="Choose an image from the manifest")
    preview_parser.add_argument("--fixtures", type=Path, help="Optional public practice examples")
    for name in ("ready", "audit"):
        sub.add_parser(name).add_argument("--file", type=Path, required=True)
    next_ = sub.add_parser("next-batch", help="Create pilot, then main pool; optionally repeats")
    next_.add_argument("--repeat-ids", type=Path, help="JSON list of previously issued photo IDs")
    next_.add_argument("--repeat-reason", help="Why independent reannotation is needed")
    for name in ("release", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--reviewer", required=True)
        command.add_argument("--reason", required=True)
        if name == "restore":
            command.add_argument("--worker", required=True)
    return result


def execute(args):
    if args.command == "select":
        rows = select_manifest(read_manifest(args.manifest), args.seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return {"selected": len(rows), "manifest": str(args.output)}
    if args.command == "plan":
        return plan(read_json(args.config))
    if args.command in {"check", "prepare"}:
        config, bank = read_json(args.config), read_json(args.fixtures)
        manifest = read_manifest(args.manifest)
        validate_inputs(manifest, bank, config)
        if args.command == "prepare":
            prepare(args.run, manifest, bank, config)
        return {"validated_images": len(manifest), "plan": plan(config)}
    if args.command == "preview" and args.manifest:
        rows = read_manifest(args.manifest)
        if args.photo_id:
            rows = [r for r in rows if str(r["photo_id"]) == args.photo_id]
        if not rows:
            raise ValueError("No image found for preview")
        bank = read_json(args.fixtures) if args.fixtures else {"practice": []}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        preview({"assets": assets(bank), "manifest": rows}, args.output)
        return {"preview": str(args.output), "photo_id": str(rows[0]["photo_id"])}
    offline = {"preview", "ready", "preflight"}
    run = Run(args.run, None if args.command in offline else TasksApi())
    if args.command == "preview":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        preview(run.snapshot, args.output)
        return {"preview": str(args.output)}
    if args.command == "ready":
        run.ready(read_json(args.file))
    elif args.command == "audit":
        run.import_audit(read_json(args.file))
    elif args.command == "next-batch":
        run.next_batch(read_json(args.repeat_ids) if args.repeat_ids else None, args.repeat_reason)
    elif args.command == "release":
        run.release(args.reviewer, args.reason)
    elif args.command == "restore":
        run.restore(args.worker, args.reviewer, args.reason)
    elif args.command in {"moderate", "close"}:
        batch = run.state["batches"][-1]
        if args.command == "moderate":
            run.api.request(
                "PUT", f"/api/new/requester/poolModeration/{batch['pool_id']}", {"status": "READY"}
            )
        else:
            run.api.wait(run.api.request("POST", f"/pools/{batch['pool_id']}/close", {}))
    else:
        return getattr(run, args.command.replace("-", "_"))()
    return {"ok": True}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with locked(args.run):
            result = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ApiError, ValueError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
