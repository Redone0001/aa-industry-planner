"""Location-aware readiness from inventory and finished job output."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from aa_industry_planner.engine import material_quantity


def remaining_materials(step):
    materials = {int(t): amount for t, amount in step.details.get("materials", {}).items()}
    completed = getattr(step, "completed_quantity", 0)
    recipe = step.details.get("recipe") or {}
    if not completed or not recipe.get("materials"):
        return materials
    needed = max(0, step.output_quantity - completed - step.ongoing_quantity)
    runs = (needed + recipe["output"] - 1) // recipe["output"]
    cap = max(step.details.get("batches") or [step.runs])
    batches = [cap] * (runs // cap) + ([runs % cap] if runs % cap else [])
    context = step.details.get("context") or {}
    efficiency = Decimal(context.get("material_multiplier", "1"))
    if step.activity == "manufacturing":
        efficiency *= 1 - Decimal(context.get("me", 0)) / 100
    return {
        int(t): sum(material_quantity(base, batch, efficiency) for batch in batches)
        for t, base in recipe["materials"].items()
    }


@dataclass
class JobInputs:
    awaiting_delivery: dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))
    delivered: dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))


def finished_job_inputs(steps, asset_dates, location_ids, target_demand):
    inputs = JobInputs()
    for step in steps:
        # Set aside output requested as a target before offering it to downstream steps.
        target = target_demand.get(step.item_type_id, 0)
        for allocation in step.allocations.all():
            job = allocation.job
            if not allocation.quantity or job.source_id not in asset_dates:
                continue
            asset_source_id = job.corporation_asset_source_id or job.source_id
            asset_date = asset_dates.get(asset_source_id)
            if job.status == "ready":
                supply = inputs.awaiting_delivery
            elif (
                job.status == "delivered"
                and job.completed_date
                and asset_source_id in asset_dates
                and (asset_date is None or asset_date < job.completed_date)
            ):
                supply = inputs.delivered
            else:
                continue
            reserved = min(target, allocation.quantity)
            target -= reserved
            if job.facility_id in location_ids and allocation.quantity > reserved:
                supply[step.item_type_id][job.facility_id] += allocation.quantity - reserved
    return inputs


def reserve_inputs(materials, destination, locations, stock, job_inputs):
    """Reserve a whole set of inputs and report which ones need delivery or hauling."""
    locations = sorted(set(locations), key=lambda loc: (loc != destination, loc))
    supplies = (stock, job_inputs.delivered, job_inputs.awaiting_delivery)
    if any(
        sum(supply.get(tid, {}).get(loc, 0) for supply in supplies for loc in locations) < amount
        for tid, amount in materials.items()
    ):
        return None

    from_stock = Counter()
    needs_haul = needs_delivery = False
    for tid, amount in materials.items():
        stock_piles, delivered_piles, delivery_piles = (supply.get(tid, {}) for supply in supplies)
        for loc in locations:
            on_hand = min(amount, stock_piles.get(loc, 0))
            delivered = min(amount - on_hand, delivered_piles.get(loc, 0))
            to_deliver = min(amount - on_hand - delivered, delivery_piles.get(loc, 0))
            if on_hand:
                stock_piles[loc] -= on_hand
                from_stock[tid] += on_hand
            if delivered:
                delivered_piles[loc] -= delivered
            if to_deliver:
                delivery_piles[loc] -= to_deliver
                needs_delivery = True
            if on_hand or delivered or to_deliver:
                amount -= on_hand + delivered + to_deliver
                needs_haul |= loc != destination
    return from_stock, needs_haul, needs_delivery


def annotate_steps(steps, stock, checked=(), available_services=None, *, job_inputs=None):
    available = {int(t): dict(piles) for t, piles in stock.items()}
    job_inputs = job_inputs or JobInputs()
    for step in steps:
        context = step.details.get("context") or {}
        destination = context.get("structure_id")
        service_available = available_services is None or step.service_id in available_services
        step.needs_haul = False
        if step.progress == 100:
            step.state, step.state_label = "complete", "Completed"
        elif step.ongoing_quantity:
            step.state, step.state_label = "active", "Job active"
        elif destination and service_available and (
            reservation := reserve_inputs(
                remaining_materials(step),
                destination,
                context.get("stock_location_ids", ()),
                available,
                job_inputs,
            )
        ) is not None:
            step.state, step.state_label = "ready", "Ready to craft"
            _, step.needs_haul, needs_delivery = reservation
            if needs_delivery:
                step.state_label += " · deliver inputs"
        else:
            step.state, step.state_label = "blocked", "Missing inputs"
        step.checked = step.item_type_id in checked


def multibuy_line(row):
    name = str(row["name"]).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return f"{name} {int(row['missing'])}"


def multibuy_text(inputs):
    """EVE Multibuy accepts an item name and an ungrouped integer per line."""
    return "\n".join(
        multibuy_line(row)
        for row in sorted(inputs, key=lambda r: r["name"].casefold())
        if row["missing"] > 0
    )
