"""Structure industry rules compiled from CCP's SDE. No database/network access.

The SDE describes the applicable target attributes, their product filters, and
the Dogma effects which modify them. In particular, a rig's displayed bonus is
not itself the final job multiplier. See docs/industry-bonuses.md.
"""

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files

D = Decimal
ONE = D(1)
ACTIVITIES = {
    "manufacturing": "Manufacturing",
    "reaction": "Reactions",
    "copying": "Copying",
    "invention": "Invention",
    "researchMaterial": "ME research",
    "researchTime": "TE research",
}
# These are the IDs in industryAssemblyLines, not the ESI job activity IDs.
LINE_ACTIVITIES = {
    1: "manufacturing",
    3: "researchTime",
    4: "researchMaterial",
    5: "copying",
    8: "invention",
    9: "reaction",
}


class FitError(ValueError):
    pass


@lru_cache(maxsize=1)
def catalog():
    return json.loads(
        files("aa_industry_planner").joinpath("data/industry_catalog.json").read_text()
    )


def type_choices(kind):
    return sorted(
        [(int(tid), t["name"]) for tid, t in catalog()["types"].items() if t["kind"] == kind],
        key=lambda row: row[1],
    )


def entry(type_id, kind=None):
    row = catalog()["types"].get(str(type_id))
    if not row or (kind and row["kind"] != kind):
        raise FitError(
            f"Type {type_id} is not a supported {kind or 'industry type'} in this SDE release."
        )
    return row


def attribute(row, attribute_id):
    key = str(attribute_id)
    return D(row["attributes"].get(key, catalog()["defaults"].get(key, "0")))


def security_band(system_id, security):
    if 31_000_000 <= system_id < 32_000_000:
        return "wormhole"
    if security is None:
        raise FitError("The solar system has no SDE security status. Reload the SDE.")
    value = D(str(security))
    if not value.is_finite() or not -1 <= value <= 1:
        raise FitError("Invalid solar-system security status.")
    return "high" if value >= D("0.45") else "low" if value > 0 else "null"


def matches(filter_row, group_id, category_id):
    # CCP lists alternatives: category OR group, never category AND group.
    return category_id in filter_row.get("categoryIDs", []) or group_id in filter_row.get(
        "groupIDs", []
    )


BONUS_SCOPE_OFFSET = 10**12


def bonus_family_choices():
    return [
        (int(fid), row["name"])
        for fid, row in sorted(catalog()["filters"].items(), key=lambda pair: int(pair[0]))
    ]


def family_activity(family_id):
    return "reaction" if int(family_id) >= 16 else "manufacturing"


def matching_bonus_families(activity, group_id, category_id):
    candidates = [
        (int(fid), row)
        for fid, row in catalog()["filters"].items()
        if family_activity(fid) == activity and matches(row, group_id, category_id)
    ]
    # An explicit product-group family wins over a broad category such as Ships.
    candidates.sort(
        key=lambda pair: (
            group_id not in pair[1]["groupIDs"],
            len(pair[1]["categoryIDs"]),
            len(pair[1]["groupIDs"]),
            pair[0],
        )
    )
    return [fid for fid, _ in candidates]


@dataclass(frozen=True)
class Multipliers:
    material: Decimal = ONE
    time: Decimal = ONE
    cost: Decimal = ONE


@dataclass(frozen=True)
class IndustryFit:
    hull_id: int
    system_id: int
    security: float | None
    modules: tuple[tuple[int, bool], ...] = ()
    rigs: tuple[int, ...] = ()
    supercapital_ready: bool = False

    @property
    def band(self):
        return security_band(self.system_id, self.security)

    def validate(self):
        hull = entry(self.hull_id, "hull")
        band = self.band
        modules = [entry(tid, "service") for tid, _ in self.modules]
        rigs = [entry(tid, "rig") for tid in self.rigs]
        if len(modules) > attribute(hull, 2056):
            raise FitError("The fitted services exceed the hull's service slots.")
        if len(rigs) > attribute(hull, 1137):
            raise FitError("The fitted rigs exceed the hull's rig slots.")
        if sum(attribute(r, 1153) for r in rigs) > attribute(hull, 1132):
            raise FitError("The fitted rigs exceed the hull's calibration capacity.")
        for row in modules + rigs:
            if hull["groupID"] not in row["fitGroups"] and self.hull_id not in row["fitTypes"]:
                raise FitError(f"{row['name']} cannot be fitted to {hull['name']}.")
            if band == "high" and attribute(row, 1970):
                raise FitError(f"{row['name']} cannot be used in high security space.")
            if attribute(row, 1074) and (band != "null" or not self.supercapital_ready):
                raise FitError(
                    f"{row['name']} requires confirmed sovereign-nullsec supercapital infrastructure."
                )
        for row in rigs:
            if attribute(row, 1547) != attribute(hull, 1547):
                raise FitError(f"{row['name']} has the wrong rig size for {hull['name']}.")
            if sum(r["groupID"] == row["groupID"] for r in rigs) > attribute(row, 1544):
                raise FitError(
                    "Only one rig per fitting group is allowed, including tech/faction variants."
                )
        counts = Counter(tid for tid, _ in self.modules)
        if any(count > attribute(entry(tid), 2431) for tid, count in counts.items()):
            raise FitError("A service module is fitted more than its type limit allows.")

    def activities(self, online_only=True):
        return {
            LINE_ACTIVITIES[line["activityID"]]
            for tid, online in self.modules
            if online or not online_only
            for line in entry(tid, "service")["assemblyLines"]
        }

    def supports(self, activity, group_id, category_id):
        for tid, online in self.modules:
            if not online:
                continue
            for line in entry(tid, "service")["assemblyLines"]:
                if LINE_ACTIVITIES[line["activityID"]] != activity:
                    continue
                groups = [r["groupID"] for r in line["detailsPerGroup"]]
                categories = [r["categoryID"] for r in line["detailsPerCategory"]]
                if (
                    (not groups and not categories)
                    or group_id in groups
                    or category_id in categories
                ):
                    return True
        return False

    def multipliers(self, activity, group_id, category_id):
        """Return facility-only multipliers for a supported product/job activity."""
        self.validate()
        if activity not in ACTIVITIES:
            raise FitError(f"Unsupported industry activity: {activity}.")
        if not self.supports(activity, group_id, category_id):
            raise FitError(
                f"No online service supports {ACTIVITIES[activity]} for product group {group_id}."
            )
        hull = entry(self.hull_id, "hull")
        sources = [hull] + [entry(tid, "rig") for tid in self.rigs]
        # Attribute IDs are deduplicated before evaluation. Two sources referring
        # to the same structure attribute must not apply its final value twice.
        targets = {metric: set() for metric in ("material", "time", "cost")}
        for source in sources:
            for metric, rules in source["bonuses"].get(activity, {}).items():
                for rule in rules:
                    if "filterID" not in rule or matches(
                        catalog()["filters"][str(rule["filterID"])], group_id, category_id
                    ):
                        targets[metric].add(rule["dogmaAttributeID"])
        values = {aid: attribute(hull, aid) for ids in targets.values() for aid in ids}
        for tid in self.rigs:
            rig = entry(tid, "rig")
            # Resolve item-local security multiplication before projecting bonuses
            # onto the structure. Do not scale hull bonuses by security.
            local = {int(k): D(v) for k, v in rig["attributes"].items()}
            local[2358] = attribute(
                rig, {"high": 2355, "low": 2356, "null": 2357, "wormhole": 2357}[self.band]
            )
            for mod in rig["modifiers"]:
                if mod["domain"] == "itemID":
                    if mod["func"] != "ItemModifier" or mod["operation"] != 4:
                        raise FitError(
                            "Unsupported item-local industry effect. Update the rules catalog."
                        )
                    aid, source = mod["modifiedAttributeID"], mod["modifyingAttributeID"]
                    local[aid] = local.get(aid, attribute(rig, aid)) * local.get(
                        source, attribute(rig, source)
                    )
            for mod in rig["modifiers"]:
                aid = mod["modifiedAttributeID"]
                if mod["domain"] == "structureID" and aid in values:
                    if mod["func"] != "ItemModifier" or mod["operation"] != 6:
                        raise FitError(
                            "Unsupported projected industry effect. Update the rules catalog."
                        )
                    source = mod["modifyingAttributeID"]
                    values[aid] *= ONE + local.get(source, attribute(rig, source)) / 100
        result = {}
        for metric, ids in targets.items():
            result[metric] = ONE
            for aid in sorted(ids):
                result[metric] *= values[aid]
        return Multipliers(**result)

    def preview(self):
        """Representative product rows; exact planning always uses actual IDs."""
        self.validate()
        rows = []
        for fid, f in catalog()["filters"].items():
            # The generic Ships filter is covered by the specific ship rows.
            if int(fid) == 3:
                continue
            category = f["categoryIDs"][0] if f["categoryIDs"] else None
            group = (
                f["groupIDs"][0]
                if f["groupIDs"]
                else next(
                    int(gid)
                    for gid, g in catalog()["groups"].items()
                    if g["categoryID"] == category
                )
            )
            category = catalog()["groups"][str(group)]["categoryID"]
            activity = "reaction" if int(fid) >= 16 else "manufacturing"
            if self.supports(activity, group, category):
                rows.append(
                    {
                        "activity": ACTIVITIES[activity],
                        "product": f["name"],
                        "multipliers": self.multipliers(activity, group, category),
                    }
                )
        if self.supports("manufacturing", 30, 6):
            rows.append(
                {
                    "activity": ACTIVITIES["manufacturing"],
                    "product": "Supercapital ships",
                    "multipliers": self.multipliers("manufacturing", 30, 6),
                }
            )
        for activity in ("copying", "invention", "researchMaterial", "researchTime"):
            # Blueprint grouping for copying/research is unrestricted; invention
            # uses a representative ship product (its SDE assembly-line category).
            if activity in self.activities():
                rows.append(
                    {
                        "activity": ACTIVITIES[activity],
                        "product": "Blueprint jobs",
                        "multipliers": self.multipliers(activity, 25, 6),
                    }
                )
        return rows
