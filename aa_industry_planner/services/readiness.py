"""Location-aware readiness from inventory and jobs awaiting delivery."""

from collections import Counter, defaultdict
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


def ready_job_inputs(steps, source_ids, location_ids, target_demand):
    inputs = defaultdict(Counter)
    for step in steps:
        # Set aside output requested as a target before offering it to downstream steps.
        target = target_demand.get(step.item_type_id, 0)
        for allocation in step.allocations.all():
            job = allocation.job
            if job.status != "ready" or job.source_id not in source_ids:
                continue
            reserved = min(target, allocation.quantity)
            target -= reserved
            if job.facility_id in location_ids and allocation.quantity > reserved:
                inputs[step.item_type_id][job.facility_id] += allocation.quantity - reserved
    return inputs


def reserve_inputs(materials, destination, locations, stock, ready_inputs):
    """Reserve a whole set of inputs and report which ones need delivery or hauling."""
    locations = sorted(set(locations), key=lambda loc: (loc != destination, loc))
    if any(
        sum(stock.get(tid, {}).get(loc, 0) + ready_inputs.get(tid, {}).get(loc, 0) for loc in locations)
        < amount
        for tid, amount in materials.items()
    ):
        return None

    from_stock = Counter()
    needs_haul = needs_delivery = False
    for tid, amount in materials.items():
        stock_piles = stock.get(tid, {})
        delivery_piles = ready_inputs.get(tid, {})
        for loc in locations:
            on_hand = min(amount, stock_piles.get(loc, 0))
            to_deliver = min(amount - on_hand, delivery_piles.get(loc, 0))
            if on_hand:
                stock_piles[loc] -= on_hand
                from_stock[tid] += on_hand
            if to_deliver:
                delivery_piles[loc] -= to_deliver
                needs_delivery = True
            if on_hand or to_deliver:
                amount -= on_hand + to_deliver
                needs_haul |= loc != destination
    return from_stock, needs_haul, needs_delivery


def annotate_steps(steps, stock, checked=(), available_services=None, *, ready_inputs=None):
    available = {int(t): dict(piles) for t, piles in stock.items()}
    pending = {tid: dict(piles) for tid, piles in (ready_inputs or {}).items()}
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
                pending,
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
