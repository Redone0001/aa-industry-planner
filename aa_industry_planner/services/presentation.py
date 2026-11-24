"""Readable tree metadata and a layered dependency graph from a saved plan."""

from collections import defaultdict, deque

NODE_WIDTH = 240
NODE_HEIGHT = 118
ROW_GAP = 26
COLUMN_GAP = 92
PADDING = 32


def plan_views(snapshot, steps):
    raw = snapshot.get("nodes", {})
    root_id = int(snapshot["product_id"])
    nodes = {}
    for key, row in raw.items():
        tid = int(key)
        if tid != root_id and not row.get("required"):
            continue
        context = row.get("context") or {}
        recipe = row.get("recipe") or {}
        step = steps.get(tid, {})
        kind = (
            recipe.get("activity") if row.get("runs") else "buy" if row.get("missing") else "stock"
        )
        nodes[tid] = {
            "id": tid,
            "name": row.get("name") or str(tid),
            "required": row.get("required", 0),
            "stock": row.get("from_stock", 0),
            "stock_locations": row.get("stock_locations", []),
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
            "me": context.get("me", 0),
            "te": context.get("te", 0),
            "width": NODE_WIDTH,
            "height": NODE_HEIGHT,
        }
    edges, children, parents = [], defaultdict(list), defaultdict(list)
    for tid in nodes:
        for child, amount in raw.get(str(tid), {}).get("materials", {}).items():
            child = int(child)
            if child in nodes and amount:
                edges.append({"source": tid, "target": child, "quantity": amount})
                children[tid].append((child, amount))
                parents[child].append(tid)
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
                shared=len(parents[tid]) > 1,
            )
    seen = set()

    def branch(tid, edge_quantity=None, level=0):
        reference = tid in seen
        seen.add(tid)
        return {
            "item": nodes[tid],
            "reference": reference,
            "edge_quantity": edge_quantity,
            "depth": level,
            "children": []
            if reference
            else [branch(child, amount, level + 1) for child, amount in children[tid]],
        }

    graph = {
        "root": root_id,
        "nodes": list(nodes.values()),
        "edges": edges,
        "width": width,
        "height": height,
    }
    return branch(root_id), graph
