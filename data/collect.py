"""Collect CC0 photo metadata; source categories are NOT human annotations."""

import argparse
import json
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from common import CANDIDATE_FIELDS, ROOT, request_json, write_csv


def photo_candidate(observation, source, seen_photos, seen_observations):
    if observation["id"] in seen_observations:
        return None
    # One CC0 photo per observation avoids filling the sample with a burst of one animal.
    for photo in observation.get("photos", []):
        if (photo.get("license_code") != "cc0" or photo.get("hidden")
                or photo.get("id") in seen_photos):
            continue
        url = photo.get("url", "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not re.search(r"/square\.[A-Za-z0-9]+$", parsed.path):
            continue
        original = parsed._replace(path=re.sub(r"/square\.", "/original.", parsed.path))
        photo_id, observation_id = photo["id"], observation["id"]
        return {
            "photo_id": photo_id, "observation_id": observation_id,
            "observation_url": f"https://www.inaturalist.org/observations/{observation_id}",
            "image_url": urllib.parse.urlunsplit(original),
            "photo_page_url": f"https://www.inaturalist.org/photos/{photo_id}",
            "license_code": photo["license_code"],
            "attribution": photo.get("attribution", ""),
            "observer_login": observation.get("user", {}).get("login", ""),
            "source_taxon_id": source["taxon_id"],
            "observation_taxon": (observation.get("taxon") or {}).get("name", ""),
            "source_bucket": source["name"], "proposed_class": source["proposed_class"],
        }
    return None


def collect(plan, output, cache):
    rng = random.Random(plan["seed"])
    rows, seen_photos, seen_observations, counts = [], set(), set(), {}
    cache.mkdir(parents=True, exist_ok=True)
    for source in plan["sources"]:
        accepted = 0
        for page in range(1, plan["max_pages_per_source"] + 1):
            params = dict(taxon_id=source["taxon_id"], photos="true", photo_license="cc0",
                          per_page=plan["per_page"], page=page, order_by="id", order="desc")
            if "without_taxon_id" in source:
                params["without_taxon_id"] = source["without_taxon_id"]
            url = "https://api.inaturalist.org/v1/observations?" + urllib.parse.urlencode(params)
            # Cached responses make a repeated run use the same candidate pool.
            snapshot = cache / f"{source['name']}-{page}.json"
            if snapshot.exists():
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
                if payload.get("request_url") != url:
                    raise ValueError(f"Cache mismatch: use a new --cache directory ({snapshot})")
                observations = payload["observations"]
            else:
                observations = request_json(url)["results"]
                # Keep only fields needed for provenance and photo-level license checks.
                observations = [{k: o.get(k) for k in ("id", "photos", "taxon", "user")}
                                for o in observations]
                snapshot.write_text(json.dumps({"request_url": url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "observations": observations}, ensure_ascii=False), encoding="utf-8")
                time.sleep(1)
            if not observations:
                break
            rng.shuffle(observations)
            for observation in observations:
                row = photo_candidate(observation, source, seen_photos, seen_observations)
                if row is None:
                    continue
                rows.append(row)
                seen_photos.add(row["photo_id"])
                seen_observations.add(row["observation_id"])
                accepted += 1
                if accepted >= source["count"]:
                    break
            write_csv(output, rows, CANDIDATE_FIELDS)
            if accepted >= source["count"]:
                break
        counts[source["name"]] = accepted
        print(f"{source['name']}: {accepted}/{source['count']}", flush=True)
    shortfall = [s["name"] for s in plan["sources"] if counts[s["name"]] < s["count"]]
    if shortfall:
        raise RuntimeError(f"Not enough candidates: {', '.join(shortfall)}. CSV kept; increase pages or revise plan.")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=ROOT / "plan.json")
    parser.add_argument("--output", type=Path, default=ROOT / "candidates.csv")
    parser.add_argument("--cache", type=Path, default=ROOT / "cache")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not 1 <= plan["per_page"] <= 200 or plan["max_pages_per_source"] < 1:
        parser.error("per_page must be 1..200 and max_pages_per_source positive")
    if any(s["count"] < 1 for s in plan["sources"]):
        parser.error("all source counts must be positive")
    if len({s["name"] for s in plan["sources"]}) != len(plan["sources"]):
        parser.error("source names must be unique")
    if args.output.exists():
        parser.error("output exists: use another --output to preserve your candidate list")
    rows = collect(plan, args.output, args.cache)
    print(f"Saved {len(rows)} candidates to {args.output}")


if __name__ == "__main__":
    main()
