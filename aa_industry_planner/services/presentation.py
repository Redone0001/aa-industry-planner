"""Readable tree metadata and a layered dependency graph from a saved plan."""

from collections import Counter, defaultdict, deque

from django.utils.dateparse import parse_datetime

NODE_WIDTH = 240
NODE_HEIGHT = 136
ROW_GAP = 26
COLUMN_GAP = 92
PADDING = 32


def plan_progress(steps):
    """Count each step in the production sequence once."""
    states = Counter(step.state for step in steps)
    total = len(steps)
    completed, in_jobs = states["complete"], states["active"]
    return dict(total=total, completed=completed, in_jobs=in_jobs, waiting=total - completed - in_jobs)


def fee_presentation(details):
    context = details.get("context") or {}
    cost = details.get("cost_details") or {}

    def date(value):
        try:
            return parse_datetime(value) if value else None
        except (TypeError, ValueError):
            return None

    return dict(
        breakdown=cost,
        index_retrieved_at=date(context.get("index_retrieved_at")),
        index_source_modified_at=date(context.get("index_source_modified_at")),
        index_cache_expires_at=date(context.get("index_cache_expires_at")),
        index_sample_at=date(context.get("index_recorded_at")),
        prices_retrieved_at=date(cost.get("prices_retrieved_at")),
        prices_source_modified_at=date(cost.get("prices_source_modified_at")),
        prices_cache_expires_at=date(cost.get("prices_cache_expires_at")),
    )


def visible_stock_details(details, transfer_names):
    def eligible_piles(row):
        return [
            dict(pile, name=transfer_names[pile["location_id"]])
            for pile in row.get("stock_elsewhere", [])
            if pile["location_id"] in transfer_names
        ]

    return dict(
        details,
        stock_elsewhere=eligible_piles(details),
        input_inventory=[
            dict(row, stock_elsewhere=eligible_piles(row))
            for row in details.get("input_inventory", [])
        ],
    )


def plan_views(snapshot, steps, checked=()):
    raw = snapshot.get("nodes", {})
    baseline = snapshot.get("before_purchases", {})
    purchases = snapshot.get("purchase_choices", {})
    roots = [int(row["type_id"]) for row in snapshot.get("targets", [])] or [
        int(snapshot["product_id"])
    ]
    nodes = {}
    for key in raw | baseline:
        tid = int(key)
        row = raw.get(key, {})
        original = baseline.get(key, {})
        required = row.get("required", 0)
        covered_quantity = max(0, original.get("required", 0) - required)
        if tid not in roots and not (required or covered_quantity):
            continue
        context = row.get("context") or {}
        recipe = row.get("recipe") or {}
        step = steps.get(tid, {})
        kind = (
            recipe.get("activity") if row.get("runs") else "buy" if row.get("missing") else "stock"
        )
        elsewhere = row.get("stock_elsewhere", [])
        state = step.get(
            "state",
            "stock"
            if kind == "stock"
            else "buy"
            if kind == "buy"
            else "complete"
            if step.get("progress") == 100
            else "blocked",
        )
        if not required and covered_quantity:
            kind = original.get("activity") or "buy"
            state = "covered"
        elif kind == "buy":
            state = "purchase" if key in purchases else "elsewhere" if elsewhere else "buy"
        nodes[tid] = {
            "id": tid,
            "name": row.get("name") or original.get("name") or str(tid),
            "required": required,
            "stock": row.get("from_stock", 0),
            "stock_locations": row.get("stock_locations", []),
            "stock_elsewhere": elsewhere,
            "elsewhere_quantity": sum(allocation["quantity"] for allocation in elsewhere),
            "covered_quantity": covered_quantity,
            "covered_step": bool(original.get("runs") and not row.get("runs") and covered_quantity),
            "removed_runs": max(0, original.get("runs", 0) - row.get("runs", 0)),
            "missing": row.get("missing", 0),
            "output": row.get("output", 0),
            "kind": kind,
            "kind_label": {
                "manufacturing": "Manufacture",
                "reaction": "Reaction",
                "buy": "Buy / import",
                "stock": "From stock",
            }.get(kind, "Produce"),
            "facility": context.get("facility", ""),
            "system": context.get("system", ""),
            "seconds": row.get("estimated_seconds", 0),
            "runs": row.get("runs", 0),
            "step": step.get("position"),
            "progress": step.get("progress", 0),
            "ongoing": step.get("ongoing", 0),
            "ongoing_quantity": step.get("ongoing_quantity", 0),
            "state": state,
            "state_label": {
                "stock": "In stock",
                "complete": "Completed",
                "ready": "Ready to craft",
                "active": "Job active",
                "buy": "Buy / import",
                "blocked": "Missing inputs",
                "elsewhere": "Stock in wrong location",
                "purchase": "Planned purchase",
                "covered": "Covered by purchase",
            }[state],
            "checked": tid in checked,
            "me": context.get("me", 0),
            "te": context.get("te", 0),
            "width": NODE_WIDTH,
            "height": NODE_HEIGHT,
        }
    edges, children, parents = [], defaultdict(list), defaultdict(list)
    active_inputs = set()
    for tid in nodes:
        materials = raw.get(str(tid), {}).get("materials", {})
        original = baseline.get(str(tid), {}).get("materials", {})
        for child, quantity in (original | materials).items():
            amount = materials.get(child, 0)
            covered = max(0, original.get(child, 0) - amount)
            child = int(child)
            if child in nodes and quantity:
                edge = {"source": tid, "target": child, "quantity": amount}
                if covered:
                    edge["covered_quantity"] = covered
                edges.append(edge)
                children[tid].append((child, edge))
                parents[child].append(tid)
                if amount:
                    active_inputs.add(child)
    indegree = {tid: len(parents[tid]) for tid in nodes}
    queue = deque(tid for tid in nodes if not indegree[tid])
    depth = dict.fromkeys(nodes, 0)
    while queue:
        tid = queue.popleft()
        for child, _ in children[tid]:
            depth[child] = max(depth[child], depth[tid] + 1)
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    columns = defaultdict(list)
    for tid in nodes:
        columns[depth[tid]].append(tid)
    # Keep adjacent layers in a similar order to reduce crossing connections.
    rank = {}
    for level in sorted(columns):
        column = columns[level]
        if level:
            column.sort(
                key=lambda tid: (
                    sum(rank.get(p, 0.5) for p in parents[tid]) / max(1, len(parents[tid])),
                    nodes[tid]["name"].casefold(),
                )
            )
        for index, tid in enumerate(column):
            rank[tid] = (index + 0.5) / len(column)
    height = (
        max((len(c) for c in columns.values()), default=1) * (NODE_HEIGHT + ROW_GAP)
        - ROW_GAP
        + 2 * PADDING
    )
    width = (max(columns, default=0) + 1) * (NODE_WIDTH + COLUMN_GAP) - COLUMN_GAP + 2 * PADDING
    for level, column in columns.items():
        column_height = len(column) * (NODE_HEIGHT + ROW_GAP) - ROW_GAP
        for index, tid in enumerate(column):
            nodes[tid].update(
                x=PADDING + level * (NODE_WIDTH + COLUMN_GAP),
                y=(height - column_height) / 2 + index * (NODE_HEIGHT + ROW_GAP),
                depth=level,
                shared=len(parents[tid]) + (tid in roots) > 1,
            )
    seen = set()

    def branch(tid, edge=None, level=0):
        reference = tid in seen or (
            edge is not None and not edge["quantity"] and tid in active_inputs
        )
        if not reference:
            seen.add(tid)
        return {
            "item": nodes[tid],
            "reference": reference,
            "edge_quantity": edge["quantity"] if edge else None,
            "edge_covered": edge.get("covered_quantity", 0) if edge else 0,
            "depth": level,
            "children": []
            if reference
            else [
                branch(child, child_edge, level + 1)
                for child, child_edge in sorted(
                    children[tid], key=lambda pair: not pair[1]["quantity"]
                )
            ],
        }

    tree = branch(roots[0])
    tree["additional_targets"] = [branch(tid) for tid in roots[1:]]
    graph = {
        "root": roots[0],
        "roots": roots,
        "nodes": list(nodes.values()),
        "edges": edges,
        "width": width,
        "height": height,
    }
    return tree, graph
