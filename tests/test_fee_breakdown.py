from decimal import Decimal as D
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from aa_industry_planner.engine import ProductionContext, Recipe, installation_cost
from aa_industry_planner.models import AdjustedPrice, SystemCostIndex
from aa_industry_planner.providers import public_cache_metadata
from aa_industry_planner.services.planning import calculate, create_plan
from aa_industry_planner.services.sync import refresh_indices, refresh_prices
from aa_industry_planner.templatetags.industry import exact_number, exact_percentage


def test_titanium_chromide_example_is_exactly_explained_by_system_component():
    recipe = Recipe(1, 101, "reaction", 200, 10800, {2: 1})
    prices = {2: D("475465.777777777778")}
    old = installation_cost(
        recipe, [18], ProductionContext(index=D(".0014"), facility_tax=D(".02")), prices
    )
    game = installation_cost(
        recipe, [18], ProductionContext(index=D(".004222"), facility_tax=D(".02")), prices
    )
    assert old["total"] == 525484 and game["total"] == 549636
    assert old["tax_fee"] == game["tax_fee"] == 513503
    assert old["system_fee"] == 11981 and game["system_fee"] == 36133
    assert game["total"] - old["total"] == game["system_fee"] - old["system_fee"] == 24152


def test_fee_breakdown_groups_identical_jobs_but_keeps_per_job_rounding():
    result = installation_cost(
        Recipe(1, 101, "reaction", 1, 60, {2: 1}),
        [1, 1, 3],
        ProductionContext(index=D(".05"), facility_tax=D(".01")),
        {2: D("10.6")},
    )
    assert result["eiv"] == D(53)
    assert result["total"] == 2
    assert result["batches"] == [
        dict(runs=1, jobs=2, eiv=D("10.6"), system_fee=D(0), tax_fee=D(0), total=D(0)),
        dict(runs=3, jobs=1, eiv=D("31.8"), system_fee=D(1), tax_fee=D(1), total=D(2)),
    ]


def test_public_imports_preserve_precision_and_separate_retrieval_from_ccp_cache(facility):
    now = timezone.now()
    modified, expires = now - timezone.timedelta(minutes=20), now + timezone.timedelta(minutes=40)
    api = Mock()
    api.indices_metadata = api.prices_metadata = dict(
        source_modified_at=modified, cache_expires_at=expires
    )
    api.indices.return_value = [
        dict(
            solar_system_id=facility.facility.solar_system_id,
            cost_indices=[dict(activity="manufacturing", cost_index="0.004222123456789")],
        )
    ]
    api.prices.return_value = [dict(type_id=5, adjusted_price="12.3456789123456")]
    refresh_indices(api, now)
    with patch("aa_industry_planner.services.sync.timezone.now", return_value=now):
        refresh_prices(api)
    index = SystemCostIndex.objects.get()
    price = AdjustedPrice.objects.get()
    assert index.cost_index == D("0.004222123456789") and price.adjusted_price == D(
        "12.3456789123456"
    )
    assert index.recorded_at == now.replace(minute=0, second=0, microsecond=0)
    assert index.retrieved_at == price.updated_at == now
    assert index.source_modified_at == price.source_modified_at == modified
    assert index.cache_expires_at == price.cache_expires_at == expires


def test_snapshot_retains_fee_prices_rates_and_cache_dates(user, recipes, assigned_facility):
    now = timezone.now()
    modified, expires = now - timezone.timedelta(minutes=20), now + timezone.timedelta(minutes=40)
    SystemCostIndex.objects.create(
        solar_system=assigned_facility.solar_system,
        activity="manufacturing",
        cost_index=D(".004222123456789"),
        recorded_at=now,
        retrieved_at=now,
        source_modified_at=modified,
        cache_expires_at=expires,
    )
    for tid in [2, 3, 4, 5]:
        AdjustedPrice.objects.create(
            type_id=tid,
            adjusted_price=D("12.3456789123456"),
            updated_at=now,
            source_modified_at=modified,
            cache_expires_at=expires,
        )
    result, snapshot = calculate(user, 1, 18, use_inventory=False)
    row = snapshot["nodes"]["1"]
    assert D(row["context"]["index"]) == D(".004222123456789")
    assert row["context"]["index_source_modified_at"] == modified.isoformat()
    assert row["context"]["index_cache_expires_at"] == expires.isoformat()
    assert row["cost_details"]["prices_source_modified_at"] == modified.isoformat()
    assert row["cost_details"]["prices_retrieved_at"] == now.isoformat()
    assert row["cost_details"]["materials"][0]["name"] == "Component A"
    assert D(row["cost_details"]["materials"][0]["adjusted_price"]) == D("12.3456789123456")
    assert result.nodes[1].estimated_job_cost == D(row["cost_details"]["total"])


def test_legacy_fee_remains_saved_until_explicit_refresh(user, recipes, client):
    production = create_plan(user, 1, 2, use_inventory=False)
    step = production.steps.get(item_type_id=1)
    step.details.pop("cost_details", None)
    step.details["estimated_job_cost"] = "1234567"
    step.save()
    client.force_login(user)
    response = client.get(reverse("aa_industry_planner:plan", args=[production.pk]))
    assert b"1.23 M ISK" in response.content
    assert b"1,234,567 ISK" in response.content
    assert b"Refresh this plan to capture" in response.content
    step.refresh_from_db()
    assert step.details["estimated_job_cost"] == "1234567"


def test_public_cache_metadata_uses_only_safe_headers():
    response = Mock(
        headers={
            "Last-Modified": "Tue, 24 Nov 2026 12:44:42 GMT",
            "Expires": "Tue, 24 Nov 2026 13:44:42 GMT",
            "Authorization": "secret",
        }
    )
    meta = public_cache_metadata(response)
    assert set(meta) == {"source_modified_at", "cache_expires_at"}
    assert meta["source_modified_at"].isoformat() == "2026-11-24T12:44:42+00:00"
    assert meta["cache_expires_at"].isoformat() == "2026-11-24T13:44:42+00:00"
    assert public_cache_metadata(Mock(headers={"Expires": "invalid"})) == {
        "source_modified_at": None,
        "cache_expires_at": None,
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.0014", "0.14%"),
        ("0.004222123456789", "0.4222123456789%"),
        ("0", "0%"),
        (None, "Unavailable"),
        ("NaN", "Unavailable"),
    ],
)
def test_fee_rates_show_the_saved_precision(value, expected):
    assert exact_percentage(value) == expected
    assert exact_number(D("8558384.0000")) == "8,558,384"
