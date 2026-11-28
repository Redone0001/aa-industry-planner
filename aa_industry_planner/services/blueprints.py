"""Blueprint and formula availability and skill requirements."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files

from aa_industry_planner.models import JOB_ACTIVITIES, IndustryJob


@dataclass
class FormulaSupply:
    available: int = 0
    reserved: list[IndustryJob] = field(default_factory=list)
    unidentified: bool = False

    @property
    def total(self):
        return self.available + len(self.reserved)


@lru_cache(maxsize=1)
def blueprint_skill_catalog():
    return json.loads(
        files("aa_industry_planner").joinpath("data/blueprint_skills.json").read_text(encoding="utf-8")
    )


def skill_copy_lists(activity, blueprint_ids):
    """Return each blueprint's skill list and the highest levels needed across the section."""
    catalog = blueprint_skill_catalog()
    names = catalog["skills"]
    requirements = catalog["activities"][activity]

    def copy_text(skills):
        ordered = sorted(skills, key=lambda skill_id: names[skill_id].casefold())
        return "\n".join(f"{names[skill_id]} {skills[skill_id]}" for skill_id in ordered)

    lists, combined = {}, {}
    for blueprint_id in blueprint_ids:
        skills = requirements.get(str(blueprint_id))
        if skills is None:
            continue
        lists[blueprint_id] = copy_text(skills)
        for skill_id, level in skills.items():
            combined[skill_id] = max(level, combined.get(skill_id, 0))
    return lists, copy_text(combined)


def installed_blueprints(user, sources, type_ids, locations):
    if not type_ids or not locations:
        return set()
    return set(
        IndustryJob.objects.filter(
            user=user,
            source__in=sources,
            activity_id=1,
            status__in=["active", "paused", "ready"],
            blueprint_type_id__in=type_ids,
            facility_id__in=locations,
        ).values_list("blueprint_type_id", "facility_id")
    )


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
