"""Compile CCP JSONL tables into the small, versioned industry rules shipped with the app.

Offline by design. Download one release's tables using the URLs in its manifest,
then run with --sde-dir and --release-file. Accepts HTTP SDE and archive keys.
"""

import argparse
import hashlib
import json
from pathlib import Path

TABLE_KEYS = {
    "types": "typeID",
    "groups": "groupID",
    "typeDogma": "typeID",
    "dogmaAttributes": "attributeID",
    "dogmaEffects": "effectID",
    "industryModifierSources": "typeID",
    "industryTargetFilters": "targetFilterID",
    "industryInstallationTypes": "typeID",
    "industryAssemblyLines": "assemblyLineID",
    "industryActivities": "activityID",
}


def build(directory, manifest):
    tables, provenance = {}, {}
    for table, key in TABLE_KEYS.items():
        path = directory / f"{table}.jsonl"
        tables[table] = {}
        for line in path.read_text().splitlines():
            row = json.loads(line)
            row[key] = row.get(key, row.get("_key"))
            tables[table][row[key]] = row
        provenance[table] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "version": manifest.get("tables", {}).get(table, {}).get("version"),
        }
    types, dogma, effects = (tables[k] for k in ("types", "typeDogma", "dogmaEffects"))

    def modifiers(tid):
        return [
            m
            for effect in dogma[tid].get("dogmaEffects", [])
            for m in effects[effect["effectID"]].get("modifierInfo", [])
        ]

    hulls = {
        tid for tid, t in types.items() if t.get("published") and t["groupID"] in {1404, 1406, 1657}
    }
    # Conversion rigs have cross-rig overrides and are irreplaceable legacy items.
    # Do not silently flatten their Dogma into ordinary Standup rig bonuses.
    rigs = {
        tid
        for tid in tables["industryModifierSources"]
        if types[tid].get("published")
        and any(e["effectID"] == 2663 for e in dogma[tid].get("dogmaEffects", []))
        and all(m["func"] == "ItemModifier" for m in modifiers(tid))
    }
    services = {
        tid
        for tid, installation in tables["industryInstallationTypes"].items()
        if types[tid].get("published")
        and any(175 <= line["assemblyLineID"] <= 184 for line in installation["assemblyLines"])
    }
    selected = hulls | rigs | services
    result = {
        "release": manifest["release"],
        "build": manifest["buildNumber"],
        "source": "https://sde.evetech.net/tranquility/",
        "tables": provenance,
        "types": {},
        "defaults": {
            str(aid): str(a.get("defaultValue", 0)) for aid, a in tables["dogmaAttributes"].items()
        },
        "filters": tables["industryTargetFilters"],
        "groups": {
            gid: {"categoryID": g["categoryID"], "name": g["name"]["en"]}
            for gid, g in tables["groups"].items()
        },
        "activities": tables["industryActivities"],
    }
    for tid in sorted(selected):
        t = types[tid]
        result["types"][tid] = {
            "name": t["name"]["en"],
            "groupID": t["groupID"],
            "kind": "hull" if tid in hulls else "rig" if tid in rigs else "service",
            "fitGroups": [
                int(a["value"])
                for a in dogma[tid].get("dogmaAttributes", [])
                if tables["dogmaAttributes"][a["attributeID"]]["name"].startswith("canFitShipGroup")
            ],
            "fitTypes": [
                int(a["value"])
                for a in dogma[tid].get("dogmaAttributes", [])
                if tables["dogmaAttributes"][a["attributeID"]]["name"].startswith("canFitShipType")
            ],
            "attributes": {
                str(a["attributeID"]): str(a["value"])
                for a in dogma[tid].get("dogmaAttributes", [])
            },
            "modifiers": modifiers(tid),
            "bonuses": {
                k: v
                for k, v in tables["industryModifierSources"].get(tid, {}).items()
                if k not in {"_key", "typeID"}
            },
            "assemblyLines": [
                tables["industryAssemblyLines"][line["assemblyLineID"]]
                for line in tables["industryInstallationTypes"]
                .get(tid, {})
                .get("assemblyLines", [])
            ],
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sde-dir", type=Path, required=True)
    parser.add_argument("--release-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = build(args.sde_dir, json.loads(args.release_file.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(f"Wrote {len(data['types'])} types from CCP release {data['release']}")
