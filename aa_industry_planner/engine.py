"""Deterministic production DAG. No Django, network or database side effects."""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import ceil

D = Decimal


class PlanningError(ValueError):
    pass


@dataclass(frozen=True)
class Recipe:
    product_id: int
    blueprint_id: int
    activity: str
    output: int
    seconds: int
    materials: dict[int, int]
    max_runs: int = 100000


@dataclass(frozen=True)
class ProductionContext:
    service_id: int | None = None
    facility: str = "Unassigned"
    structure_id: int | None = None
    system: str = ""
    index: Decimal | None = None
    index_recorded_at: str | None = None
    material_multiplier: Decimal = D(1)
    time_multiplier: Decimal = D(1)
    cost_multiplier: Decimal = D(1)
    facility_tax: Decimal = D("0.0025")
    scc_surcharge: Decimal = D("0.04")
    alpha_tax: Decimal = D(0)
    me: int = 0
    te: int = 0
    reaction_runs: int = 1
    reaction_hours: int | None = None
    bonus_source: str = "Unassigned facility"
    facility_fit: dict | None = None
    stock_location_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class StockScope:
    location_ids: tuple[int, ...] = ()
    destination_id: int | None = None
    activity: str = ""


def stock_requirement(quantity, scope, consumer_id=None):
    return dict(
        quantity=quantity,
        location_ids=list(scope.location_ids),
        destination_id=scope.destination_id,
        activity=scope.activity,
        consumer_id=consumer_id,
        from_stock=0,
        allocations={},
    )


def allocate_location_stock(requirements, available):
    """Maximum stock coverage with shared storage capacities; each pile is used once."""
    locations = sorted(loc for loc, quantity in available.items() if quantity > 0)
    location_set = set(locations)
    sink = 1 + len(requirements) + len(locations)
    edges = [[] for _ in range(sink + 1)]

    def edge(start, end, capacity):
        forward = [end, capacity, len(edges[end])]
        backward = [start, 0, len(edges[start])]
        edges[start].append(forward)
        edges[end].append(backward)
        return forward

    location_nodes = {loc: 1 + len(requirements) + i for i, loc in enumerate(locations)}
    links = []
    for index, requirement in enumerate(requirements, 1):
        edge(0, index, requirement["quantity"])
        # Prefer stock already at the destination before drawing from storage.
        eligible = sorted(
            set(requirement["location_ids"]) & location_set,
            key=lambda loc: (loc != requirement["destination_id"], loc),
        )
        for loc in eligible:
            link = edge(index, location_nodes[loc], requirement["quantity"])
            links.append((requirement, loc, link))
    for loc, node in location_nodes.items():
        edge(node, sink, available[loc])
    while True:
        previous = {0: None}
        queue = deque([0])
        while queue and sink not in previous:
            start = queue.popleft()
            for index, (end, capacity, _) in enumerate(edges[start]):
                if capacity and end not in previous:
                    previous[end] = (start, index)
                    queue.append(end)
        if sink not in previous:
            break
        amount, end = None, sink
        while end:
            start, index = previous[end]
            amount = (
                edges[start][index][1] if amount is None else min(amount, edges[start][index][1])
            )
            end = start
        end = sink
        while end:
            start, index = previous[end]
            link = edges[start][index]
            link[1] -= amount
            edges[end][link[2]][1] += amount
            end = start
    for requirement, loc, link in links:
        allocated = requirement["quantity"] - link[1]
        if allocated:
            requirement["allocations"][loc] = allocated
            requirement["from_stock"] += allocated
    return sum(requirement["from_stock"] for requirement in requirements)


@dataclass
class Node:
    type_id: int
    required: int
    from_stock: int
    missing: int
    runs: int = 0
    output: int = 0
    batches: list[int] = field(default_factory=list)
    materials: dict[int, int] = field(default_factory=dict)
    recipe: Recipe | None = None
    context: ProductionContext | None = None
    estimated_job_cost: Decimal | None = None
    estimated_seconds: int = 0
    warnings: list[str] = field(default_factory=list)
    inventory_requirements: list[dict] = field(default_factory=list)


@dataclass
class Result:
    nodes: dict[int, Node]
    order: list[int]  # Inputs first, final product last.


def material_quantity(base, runs, efficiency):
    # EVE rounds to two decimals before ceiling and requires at least one unit/run.
    adjusted = (D(base) * runs * efficiency).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    return max(runs, int(adjusted.to_integral_value(rounding=ROUND_CEILING)))


def plan(
    product_id,
    quantity,
    recipe_for,
    context_for,
    stock=None,
    buy=None,
    prices=None,
    *,
    stock_by_location=None,
    root_stock_scope=None,
):
    if not 0 < quantity <= 10**12:
        raise PlanningError("Quantity must be between 1 and 1,000,000,000,000.")
    stock, buy, prices = stock or {}, set(buy or ()), prices or {}
    recipes, graph, visiting = {}, {}, set()

    def visit(type_id):
        if type_id in visiting:
            raise PlanningError(
                f"Recipe cycle detected at type {type_id}. Mark an item as buy/import."
            )
        if type_id in graph:
            return
        if len(graph) > 1500 or len(visiting) > 100:
            raise PlanningError("Production chain is too large.")
        visiting.add(type_id)
        recipe = None if type_id in buy else recipe_for(type_id)
        if recipe and (
            recipe.output <= 0
            or recipe.seconds <= 0
            or not recipe.materials
            or any(v <= 0 for v in recipe.materials.values())
        ):
            raise PlanningError(f"Incomplete recipe for type {type_id}. Refresh the SDE.")
        recipes[type_id] = recipe
        graph[type_id] = tuple(recipe.materials) if recipe else ()
        for child in graph[type_id]:
            visit(child)
        visiting.remove(type_id)

    visit(product_id)
    indegree = defaultdict(int)
    for children in graph.values():
        for child in children:
            indegree[child] += 1
    queue = deque(t for t in graph if not indegree[t])
    order = []
    while queue:
        type_id = queue.popleft()
        order.append(type_id)
        for child in graph[type_id]:
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)

    demand, nodes = defaultdict(int, {product_id: quantity}), {}
    requirements = defaultdict(list)
    if stock_by_location is not None:
        requirements[product_id].append(
            stock_requirement(quantity, root_stock_scope or StockScope())
        )
    for type_id in order:
        required = demand[type_id]
        allocated = (
            allocate_location_stock(requirements[type_id], stock_by_location.get(type_id, {}))
            if stock_by_location is not None
            else min(required, max(0, stock.get(type_id, 0)))
        )
        node = Node(type_id, required, allocated, required - allocated, recipe=recipes[type_id])
        node.inventory_requirements = requirements[type_id]
        nodes[type_id] = node
        if not node.missing or not node.recipe:
            continue
        recipe = node.recipe
        ctx = node.context = context_for(type_id, recipe)
        node.runs = ceil(node.missing / recipe.output)
        per_run = D(recipe.seconds) * ctx.time_multiplier
        if recipe.activity == "manufacturing":
            per_run *= 1 - D(ctx.te) / 100
        if recipe.activity == "reaction":
            batch = (
                max(1, int(D(ctx.reaction_hours * 3600) / per_run))
                if ctx.reaction_hours
                else max(1, ctx.reaction_runs)
            )
            node.runs = ceil(node.runs / batch) * batch
        else:
            batch = node.runs
        # Respect the SDE production limit and EVE's 30-day installation limit.
        limit = min(max(1, recipe.max_runs), max(1, int(D(30 * 86400) / per_run)))
        batch = min(batch, limit)
        if ceil(node.runs / batch) > 10000:
            raise PlanningError("This plan requires too many jobs. Reduce the requested quantity.")
        remaining = node.runs
        while remaining:
            size = min(remaining, batch)
            node.batches.append(size)
            remaining -= size
        node.output = node.runs * recipe.output
        efficiency = ctx.material_multiplier
        if recipe.activity == "manufacturing":
            efficiency *= 1 - D(ctx.me) / 100
        for material, base in recipe.materials.items():
            amount = sum(material_quantity(base, size, efficiency) for size in node.batches)
            node.materials[material] = amount
            demand[material] += amount
            if stock_by_location is not None:
                requirements[material].append(
                    stock_requirement(
                        amount,
                        StockScope(ctx.stock_location_ids, ctx.structure_id, recipe.activity),
                        type_id,
                    )
                )
        node.estimated_seconds = int(
            (per_run * node.runs).to_integral_value(rounding=ROUND_CEILING)
        )
        if ctx.service_id is None:
            node.warnings.append(
                "No facility assigned; duration and material estimates use neutral facility bonuses."
            )
        if ctx.index is None:
            node.warnings.append(
                "System cost index is unavailable; installation cost cannot be estimated."
            )
        missing_prices = [t for t in recipe.materials if t not in prices or prices[t] is None]
        if missing_prices:
            node.warnings.append(
                "Adjusted material prices are missing; installation cost cannot be estimated."
            )
        if ctx.index is not None and not missing_prices:
            eiv = sum((D(base) * prices[t] for t, base in recipe.materials.items()), D(0))
            node.estimated_job_cost = sum(
                (
                    (eiv * size * ctx.index * ctx.cost_multiplier).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                    + (
                        eiv * size * (ctx.facility_tax + ctx.scc_surcharge + ctx.alpha_tax)
                    ).to_integral_value(rounding=ROUND_FLOOR)
                    for size in node.batches
                ),
                D(0),
            )
    return Result(nodes=nodes, order=list(reversed(order)))
