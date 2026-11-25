"""Presence of blueprints and reusable reaction formulas at job locations."""

from collections import defaultdict
from dataclasses import dataclass, field

from aa_industry_planner.models import JOB_ACTIVITIES, IndustryJob


@dataclass
class FormulaSupply:
    available: int = 0
    reserved: list[IndustryJob] = field(default_factory=list)
    unidentified: bool = False

    @property
    def total(self):
        return self.available + len(self.reserved)


def reaction_formula_key(step):
    location = (step.details.get("context") or {}).get("structure_id")
    if location is None and step.service_id:
        location = step.service.facility.structure_id
    return step.blueprint_type_id, location


def reaction_formula_inventory(user, sources, keys, assets):
    formulas = {key: FormulaSupply() for key in keys}
    if not formulas:
        return formulas

    items = defaultdict(dict)
    for asset, location in assets:
        key = asset["type_id"], location
        if key in formulas and asset["quantity"] and not asset["is_blueprint_copy"]:
            items[key][asset["item_id"]] = asset["quantity"]

    jobs = IndustryJob.objects.filter(
        user=user,
        source__in=sources,
        activity_id__in=[aid for aid, activity in JOB_ACTIVITIES.items() if activity == "reaction"],
        status__in=["active", "paused", "ready"],
        blueprint_type_id__in={type_id for type_id, _ in keys},
        facility_id__in={location for _, location in keys},
    ).order_by("-start_date", "-job_id")
    reserved_items = set()
    unidentified = defaultdict(int)
    for job in jobs:
        key = job.blueprint_type_id, job.facility_id
        if key not in formulas:
            continue
        item_id = job.blueprint_item_id
        if item_id:
            if item_id in reserved_items:
                continue
            reserved_items.add(item_id)
            if item_id in items[key]:
                items[key][item_id] -= 1
        else:
            unidentified[key] += 1
        formulas[key].reserved.append(job)

    for key, supply in formulas.items():
        # Pre-upgrade jobs can overlap with imported assets. Count the minimum
        # number of distinct formulas until a job refresh gives us item IDs.
        supply.available = max(0, sum(items[key].values()) - unidentified[key])
        supply.unidentified = bool(unidentified[key])
    return formulas
