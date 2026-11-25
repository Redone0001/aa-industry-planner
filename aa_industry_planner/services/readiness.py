"""Location-aware readiness from imported stock, without assuming future outputs."""

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


def annotate_steps(steps, stock, checked=(), available_services=None):
    available = {int(t): dict(piles) for t, piles in stock.items()}
    for step in steps:
        context = step.details.get("context") or {}
        destination = context.get("structure_id")
        locations = sorted(
            set(context.get("stock_location_ids", ())), key=lambda loc: loc != destination
        )
        materials = remaining_materials(step)
        service_available = available_services is None or step.service_id in available_services
        ready = (
            bool(destination)
            and service_available
            and all(
                sum(available.get(t, {}).get(loc, 0) for loc in locations) >= amount
                for t, amount in materials.items()
            )
        )
        step.needs_haul = False
        if step.progress == 100:
            step.state, step.state_label = "complete", "Completed"
        elif step.ongoing_quantity:
            step.state, step.state_label = "active", "Job active"
        elif ready:
            step.state, step.state_label = "ready", "Ready to craft"
            # Reserve only whole ready steps, in production order, within this view.
            for t, amount in materials.items():
                for loc in locations:
                    used = min(amount, available.get(t, {}).get(loc, 0))
                    if used:
                        available[t][loc] -= used
                        amount -= used
                        step.needs_haul |= loc != destination
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
