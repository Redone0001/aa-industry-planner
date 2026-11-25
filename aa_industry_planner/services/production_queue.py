"""Next batches and material shortages across a user's saved production plans."""

from collections import Counter, defaultdict

from eve_sde.models import ItemType

from aa_industry_planner.engine import (
    StockScope,
    allocate_location_stock,
    locate_stock_elsewhere,
    stock_requirement,
)
from aa_industry_planner.models import Facility, FacilityService

from .inventory import inventory_by_location, stock_location_names
from .locations import InventoryLocations
from .readiness import multibuy_line
from .scheduling import remaining_work, saved_targets, target_inventory, target_production


def next_batch(work, scope, stock):
    eligible = sorted(scope.location_ids, key=lambda loc: (loc != scope.destination_id, loc))
    available = {
        tid: sum(stock.get(tid, {}).get(loc, 0) for loc in eligible)
        for tid in work.remaining_inputs()
    }
    # Match the forecast: try a full batch first, then its smaller tail.
    for runs in work.batches:
        inputs = work.inputs_for_batch(runs)
        if any(available[tid] < quantity for tid, quantity in inputs.items()):
            continue
        transfers = False
        for tid, quantity in inputs.items():
            pile = stock.get(tid, {})
            for loc in eligible:
                used = min(quantity, pile.get(loc, 0))
                if used:
                    pile[loc] -= used
                    quantity -= used
                    transfers |= loc != scope.destination_id
        return runs, inputs, transfers
    return None


def production_queue(user, plans):
    locations = InventoryLocations(user)
    location_ids = set(locations.all_ids)
    stock = inventory_by_location(user, locations, include_elsewhere=True)
    services = set(
        FacilityService.objects.filter(
            online=True,
            facility__in=Facility.visible_to(user),
        ).values_list("pk", flat=True)
    )
    names, requirements = {}, defaultdict(list)
    ready, waiting, running, delivery = [], [], [], []

    def require(tid, amount, scope, production):
        if amount > 0:
            requirements[tid].append(dict(stock_requirement(amount, scope), plan=production))

    for production in plans:
        snapshot = production.snapshot
        nodes = snapshot.get("nodes", {})
        names.update({int(tid): node["name"] for tid, node in nodes.items() if node.get("name")})
        for target in saved_targets(snapshot):
            tid = target["type_id"]
            node = nodes.get(str(tid), {})
            direct = target_inventory(node)
            scope = (
                StockScope(
                    tuple(location_ids.intersection(direct["location_ids"])),
                    direct["destination_id"],
                )
                if direct
                else locations.item_scope(tid)
            )
            amount = direct["from_stock"] if direct else node.get("from_stock", 0)
            require(tid, amount if node.get("runs") else target["quantity"], scope, production)

    # A batch cannot start with units that already fulfil another requested output.
    for tid, rows in requirements.items():
        piles = stock.get(tid, {})
        allocate_location_stock(rows, piles)
        pending = []
        for row in rows:
            for loc, amount in row["allocations"].items():
                piles[loc] -= amount
            outstanding = row["quantity"] - row["from_stock"]
            if outstanding:
                pending.append(dict(row, quantity=outstanding, from_stock=0, allocations={}))
        requirements[tid] = pending

    for production in plans:
        work, _ = remaining_work(
            production.steps.all(),
            target_demand=target_production(production.snapshot),
            include_external=True,
        )
        for step_work in sorted(work.values(), key=lambda row: row.step.position):
            step = step_work.step
            context = step.details.get("context") or {}
            scope = StockScope(
                tuple(location_ids.intersection(context.get("stock_location_ids", ()))),
                context.get("structure_id"),
                step.activity,
            )
            label = dict(plan=production, step=step, facility=context.get("facility"))
            jobs = [allocation.job for allocation in step.allocations.all() if allocation.quantity]
            if delivery_jobs := [job for job in jobs if job.status == "ready"]:
                delivery.append(dict(label, jobs=delivery_jobs))
            if running_jobs := [job for job in jobs if job.status in {"active", "paused"}]:
                running.append(dict(label, jobs=running_jobs))
            if not step_work.batches:
                continue

            batch_inputs = {}
            if not scope.destination_id or step.service_id not in services:
                waiting.append(
                    dict(
                        label,
                        reason="Assign an available production facility and refresh the plan.",
                    )
                )
            elif batch := next_batch(step_work, scope, stock):
                runs, batch_inputs, transfers = batch
                ready.append(dict(label, runs=runs, transfers=transfers))
            else:
                waiting.append(
                    dict(label, reason="Waiting for inputs at production or storage locations.")
                )

            for tid, amount in step_work.remaining_inputs().items():
                # For intermediates, acquire only the portion the saved plan expects from stock.
                # The rest is supplied by this plan's own production steps.
                if tid in work:
                    amount = min(amount, step_work.stock[tid])
                require(tid, amount - batch_inputs.get(tid, 0), scope, production)

    shortages = []
    for tid, rows in requirements.items():
        piles = stock.get(tid, {})
        allocate_location_stock(rows, piles)
        # Consume eligible stock across all rows before proposing any transfers.
        movable = [row for row in rows if row["location_ids"]]
        if piles and any(row["quantity"] > row["from_stock"] for row in movable):
            locate_stock_elsewhere(movable, piles)
        by_plan = {}
        transfers = Counter()
        for row in rows:
            uncovered = row["quantity"] - row["from_stock"]
            if not uncovered:
                continue
            elsewhere = row.get("elsewhere", {})
            move = sum(elsewhere.values())
            plan = row["plan"]
            allocation = by_plan.setdefault(plan.pk, dict(plan=plan, buy=0, move=0))
            allocation["buy"] += uncovered - move
            allocation["move"] += move
            transfers.update(elsewhere)
        if by_plan:
            shortages.append(
                dict(
                    type_id=tid,
                    missing=sum(row["buy"] for row in by_plan.values()),
                    move=sum(transfers.values()),
                    transfers=transfers,
                    plans=list(by_plan.values()),
                )
            )

    names.update(
        ItemType.objects.filter(
            pk__in={row["type_id"] for row in shortages} - names.keys()
        ).values_list("pk", "name")
    )
    transfer_names = stock_location_names(
        user, {loc for row in shortages for loc in row["transfers"]}
    )
    for row in shortages:
        row["name"] = names.get(row["type_id"], str(row["type_id"]))
        row["multibuy_line"] = multibuy_line(row)
        row["transfers"] = [
            (transfer_names[loc], amount) for loc, amount in row["transfers"].items()
        ]
    shortages.sort(key=lambda row: row["name"].casefold())
    return dict(
        ready=ready, waiting=waiting, running=running, delivery=delivery, shortages=shortages
    )
