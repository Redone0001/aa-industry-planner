"""Saved Janice input prices and EVE job installation fees."""

from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from aa_industry_planner.models import ProductionPlan

JANICE_PRICER = "https://janice.e-351.com/api/rest/v2/pricer"


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
    prices = {}
    for row in rows:
        try:
            type_id = str(row["itemType"]["eid"])
            price = Decimal(str(row["immediatePrices"]["sellPrice"]))
        except (KeyError, TypeError, InvalidOperation):
            continue
        if type_id in type_ids and price.is_finite() and 0 < price < Decimal("1e30"):
            prices[type_id] = str(price)
    return prices


def refresh_market_prices(production):
    """Fetch quotes on explicit plan refresh, after the plan transaction commits."""
    api_key = getattr(settings, "JANICE_API_KEY", "")
    type_ids = purchase_quantities(production.snapshot["nodes"])
    if not api_key or not type_ids:
        return
    previous = production.snapshot.get("market_prices", {})
    try:
        prices = _janice_prices(api_key, type_ids)
    except (requests.RequestException, ValueError):
        quotes = dict(previous, refresh_failed=True)
    else:
        quotes = dict(prices=prices, fetched_at=timezone.now().isoformat())
    snapshot = dict(production.snapshot, market_prices=quotes)
    # A later plan calculation takes priority over a slower market request.
    ProductionPlan.objects.filter(
        pk=production.pk, refreshed_at=production.refreshed_at, archived=False
    ).update(snapshot=snapshot)


def plan_cost(snapshot):
    nodes = snapshot.get("nodes", {})
    quotes = snapshot.get("market_prices", {})
    prices = quotes.get("prices", {})
    inputs = purchase_quantities(nodes)
    priced_inputs = inputs.keys() & prices.keys()
    input_cost = sum((Decimal(prices[tid]) * inputs[tid] for tid in priced_inputs), Decimal(0))
    fees = [node.get("estimated_job_cost") for node in nodes.values() if node["runs"]]
    priced_fees = [Decimal(fee) for fee in fees if fee is not None]
    fees_cost = sum(priced_fees, Decimal(0))
    missing_inputs = len(inputs) - len(priced_inputs)
    missing_fees = len(fees) - len(priced_fees)
    show_total = priced_inputs or priced_fees or not (missing_inputs or missing_fees)
    fetched_at = quotes.get("fetched_at")
    return dict(
        input_cost=input_cost if priced_inputs or not inputs else None,
        fees=fees_cost if priced_fees or not fees else None,
        total=input_cost + fees_cost if show_total else None,
        missing_inputs=missing_inputs,
        missing_fees=missing_fees,
        fetched_at=parse_datetime(fetched_at) if fetched_at else None,
        refresh_failed=quotes.get("refresh_failed", False),
        janice_configured=bool(getattr(settings, "JANICE_API_KEY", "")),
    )
