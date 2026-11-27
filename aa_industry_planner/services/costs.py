"""Value saved production plans and their steps using Janice market quotes."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from aa_industry_planner.models import ProductionPlan

JANICE_PRICER = "https://janice.e-351.com/api/rest/v2/pricer"
MARKET_SIDES = ("buy", "sell")
ZERO = Decimal(0)


@dataclass(frozen=True)
class Estimate:
    value: Decimal | None
    missing: int = 0


def purchase_quantities(nodes):
    return {
        type_id: node["missing"]
        for type_id, node in nodes.items()
        if node["missing"] and not node["runs"]
    }


def _janice_prices(api_key, type_ids):
    response = requests.post(
        JANICE_PRICER,
        params={"market": 2},
        headers={"X-ApiKey": api_key, "Content-Type": "text/plain", "Accept": "application/json"},
        data="\n".join(sorted(type_ids, key=int)).encode("ascii"),
        timeout=(2, 5),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError("Janice price request failed.")
    rows = response.json(parse_float=Decimal)
    if not isinstance(rows, list):
        raise ValueError("Unexpected Janice response.")
    prices = {side: {} for side in MARKET_SIDES}
    for row in rows:
        try:
            type_id = str(row["itemType"]["eid"])
            immediate = row["immediatePrices"]
        except (KeyError, TypeError):
            continue
        if type_id not in type_ids or not isinstance(immediate, dict):
            continue
        for side in MARKET_SIDES:
            try:
                price = Decimal(str(immediate[f"{side}Price"]))
            except (KeyError, InvalidOperation):
                continue
            if price.is_finite() and 0 < price < Decimal("1e30"):
                prices[side][type_id] = str(price)
    return prices


def refresh_market_prices(production):
    """Fetch quotes on explicit plan refresh, after the plan transaction commits."""
    api_key = getattr(settings, "JANICE_API_KEY", "")
    nodes = production.snapshot["nodes"]
    type_ids = set(purchase_quantities(nodes))
    for tid, node in nodes.items():
        if node["runs"]:
            type_ids.add(tid)
            type_ids.update(node["materials"])
    if not api_key or not type_ids:
        return
    previous = production.snapshot.get("market_prices", {})
    try:
        prices = _janice_prices(api_key, type_ids)
    except (requests.RequestException, ValueError):
        quotes = dict(previous, refresh_failed=True)
    else:
        quotes = dict(
            buy_prices=prices["buy"],
            sell_prices=prices["sell"],
            fetched_at=timezone.now().isoformat(),
        )
    snapshot = dict(production.snapshot, market_prices=quotes)
    # A later plan calculation takes priority over a slower market request.
    ProductionPlan.objects.filter(
        pk=production.pk, refreshed_at=production.refreshed_at, archived=False
    ).update(snapshot=snapshot)


def _production_inputs(snapshot, purchases):
    """Split each step's materials into market-valued and made units."""
    nodes = snapshot.get("nodes", {})
    all_inputs = Counter(purchases)
    steps = {}
    allocated_stock = {
        (str(row["consumer_id"]), tid): row["from_stock"]
        for tid, node in nodes.items()
        for row in node.get("inventory_requirements", ())
        if row["consumer_id"] is not None
    }
    # Older plans had only plan-wide stock totals. Distribute those once in saved step order.
    pooled_stock = {
        tid: node.get("from_stock", 0)
        for tid, node in nodes.items()
        if not node.get("inventory_requirements")
    }
    for tid in map(str, snapshot.get("order", ())):
        node = nodes[tid]
        if not node["runs"]:
            continue
        market, made = {}, {}
        for child, quantity in node["materials"].items():
            from_stock = allocated_stock.get((tid, child), 0)
            if child in pooled_stock:
                from_stock = min(quantity, pooled_stock[child])
                pooled_stock[child] -= from_stock
            if from_stock:
                all_inputs[child] += from_stock
            if nodes[child]["runs"] and quantity > from_stock:
                made[child] = quantity - from_stock
            market_quantity = quantity - made.get(child, 0)
            if market_quantity:
                market[child] = market_quantity
        steps[tid] = (market, made)
    return all_inputs, steps


def _market_value(quantities, prices):
    priced = quantities.keys() & prices.keys()
    missing = len(quantities) - len(priced)
    value = sum((prices[tid] * quantities[tid] for tid in priced), ZERO)
    return Estimate(value if priced or not quantities else None, missing)


def _step_values(nodes, step_inputs, fees, prices):
    values = {}
    # Saved order lists inputs before the steps that consume them.
    for tid, (market, made) in step_inputs.items():
        own_inputs = _market_value(market, prices)
        upstream = [
            (values[child]["cost"], units, nodes[child]["output"]) for child, units in made.items()
        ]
        cost = None
        if (
            not own_inputs.missing
            and fees[tid] is not None
            and all(value is not None for value, _, _ in upstream)
        ):
            cost = (
                own_inputs.value
                + fees[tid]
                + sum((value * units / output for value, units, output in upstream), ZERO)
            )
        output_value = prices[tid] * nodes[tid]["output"] if tid in prices else None
        values[tid] = dict(
            cost=cost,
            output=output_value,
            ratio=output_value / cost if output_value is not None and cost else None,
        )
    return values


def plan_cost(snapshot):
    nodes = snapshot.get("nodes", {})
    quotes = snapshot.get("market_prices", {})
    purchases = purchase_quantities(nodes)
    all_inputs, step_inputs = _production_inputs(snapshot, purchases)
    surplus_quantities = {
        tid: node["output"] - node["missing"]
        for tid, node in nodes.items()
        if node["runs"] and node["output"] > node["missing"]
    }
    surplus_rows = {
        tid: dict(name=nodes[tid].get("name", tid), quantity=quantity, markets={})
        for tid, quantity in surplus_quantities.items()
    }
    fees = {tid: node.get("estimated_job_cost") for tid, node in nodes.items() if node["runs"]}
    fees = {tid: Decimal(fee) if fee is not None else None for tid, fee in fees.items()}
    priced_fees = [fee for fee in fees.values() if fee is not None]
    missing_fees = len(fees) - len(priced_fees)
    fee_total = Estimate(sum(priced_fees, ZERO) if priced_fees or not fees else None, missing_fees)
    markets = {}
    step_values = {}
    missing_quotes = {}
    surplus_values = {}
    for side in MARKET_SIDES:
        # v0.6.0 saved only the sell side under "prices".
        saved = quotes.get(f"{side}_prices", quotes.get("prices", {}) if side == "sell" else {})
        prices = {tid: Decimal(price) for tid, price in saved.items()}
        inputs = _market_value(all_inputs, prices)
        known = [value for value in (inputs.value, fee_total.value) if value is not None]
        markets[side] = dict(
            inputs=inputs,
            purchases=_market_value(purchases, prices),
            total=Estimate(sum(known, ZERO) if known else None, inputs.missing + missing_fees),
        )
        surplus_values[side] = _market_value(surplus_quantities, prices)
        for tid, row in surplus_rows.items():
            row["markets"][side] = prices[tid] * row["quantity"] if tid in prices else None
        step_values[side] = _step_values(nodes, step_inputs, fees, prices)
        missing_quotes[side] = len((all_inputs.keys() | step_inputs.keys()) - prices.keys())
    fetched_at = quotes.get("fetched_at")
    return dict(
        markets=markets,
        fees=fee_total,
        steps=step_values,
        surplus=dict(
            rows=sorted(surplus_rows.values(), key=lambda row: row["name"].casefold()),
            markets=surplus_values,
        ),
        missing_quotes=missing_quotes,
        missing_fees=missing_fees,
        fetched_at=parse_datetime(fetched_at) if fetched_at else None,
        refresh_failed=quotes.get("refresh_failed", False),
        janice_configured=bool(getattr(settings, "JANICE_API_KEY", "")),
    )
