"""Compile manufacturing and reaction skill requirements from one CCP SDE release."""

import argparse
import json
from pathlib import Path


def build(blueprints_path, types_path, release):
    activities = {activity: {} for activity in ("manufacturing", "reaction")}
    skill_ids = set()
    with blueprints_path.open(encoding="utf-8") as blueprints:
        for line in blueprints:
            row = json.loads(line)
            for activity in activities.keys() & row["activities"].keys():
                requirements = {
                    skill["typeID"]: skill["level"]
                    for skill in row["activities"][activity].get("skills", [])
                    if skill["level"] > 0
                }
                activities[activity][row["blueprintTypeID"]] = requirements
                skill_ids.update(requirements)

    skills = {}
    with types_path.open(encoding="utf-8") as types:
        for line in types:
            row = json.loads(line)
            type_id = row.get("typeID", row.get("_key"))
            if type_id in skill_ids:
                skills[type_id] = row["name"]["en"]
    if missing := skill_ids - skills.keys():
        raise ValueError(f"Missing English skill names for {sorted(missing)}")

    return {"release": release, "activities": activities, "skills": skills}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blueprints", type=Path, required=True)
    parser.add_argument("--types", type=Path, required=True)
    parser.add_argument("--release-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.release_file.read_text(encoding="utf-8"))
    data = build(args.blueprints, args.types, manifest["release"])
    args.output.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
