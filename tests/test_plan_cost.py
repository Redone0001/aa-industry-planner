from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from requests import Timeout

from aa_industry_planner.models import (
    AdjustedPrice,
    InventorySource,
    ItemPolicy,
    ManualStock,
    SystemCostIndex,
)
from aa_industry_planner.services.costs import Estimate, plan_cost
from aa_industry_planner.services.planning import calculate_targets, create_plan

from .conftest import add_recipe


def test_explicit_refresh_prices_inputs_and_outputs_and_preserves_quotes_on_failure(
    user, client, types, assigned_facility, settings, django_capture_on_commit_callbacks
):
    settings.JANICE_API_KEY = "test-janice-key"
    add_recipe(types, 1, {2: 1, 5: 2})
    add_recipe(types, 2, {5: 3, 3: 1})
    source = InventorySource.objects.create(user=user, kind="manual", name="On hand")
    ManualStock.objects.create(
        source=source, item_type=types[5], facility=assigned_facility, quantity=1
    )
    SystemCostIndex.objects.create(
        solar_system=assigned_facility.solar_system,
        activity="manufacturing",
        cost_index=Decimal(".1"),
    )
    for tid, price in {2: 45, 3: 30, 5: 100}.items():
        AdjustedPrice.objects.create(type_id=tid, adjusted_price=price)
    client.force_login(user)
    with patch("aa_industry_planner.services.costs.requests.post") as post:
        plan = create_plan(user, 1, 1, default_me=0)
        detail_url = reverse("aa_industry_planner:plan", args=[plan.pk])
        refresh_url = reverse("aa_industry_planner:refresh_plan", args=[plan.pk])
        initial = client.get(detail_url)
        assert b"Refresh this plan to fetch prices" in initial.content
        assert initial.context["cost"]["rows"]["Production fees"] == [Estimate(81)]
        post.assert_not_called()

        post.return_value.status_code = 200
        post.return_value.json.return_value = [
            {
                "itemType": {"eid": tid},
                "immediatePrices": {"buyPrice": buy, "sellPrice": sell},
            }
            for tid, buy, sell in [
                (1, 414, 727),
                (2, 500, 700),
                (3, 120, 180),
                (5, 15, Decimal("20.5")),
            ]
        ]
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(refresh_url).status_code == 302
        assert post.call_args.kwargs["data"] == b"1\n2\n3\n5"
        assert post.call_args.kwargs["params"] == {"market": 2}
        assert post.call_args.kwargs["headers"]["X-ApiKey"] == "test-janice-key"
        detail = client.get(detail_url)
        assert detail.content.index(b'id="production-timing"') < detail.content.index(
            b'id="production-cost"'
        )
        assert b"test-janice-key" not in detail.content
        costs = detail.context["cost"]
        assert costs["rows"]["All inputs"] == [Estimate(195), Estimate(Decimal("282.5"))]
        assert costs["rows"]["Missing inputs to buy"] == [Estimate(180), Estimate(262)]
        assert costs["rows"]["Total production cost"] == [Estimate(276), Estimate(Decimal("363.5"))]
        assert costs["steps"]["buy"]["1"] == {"output": 414, "cost": 276, "ratio": Decimal("1.5")}
        assert costs["steps"]["sell"]["1"]["ratio"] == 2
        assert b"Jita buy 150.00%" in detail.content
        assert b"Jita sell 200.00%" in detail.content
        post.assert_called_once()

        purchase_url = reverse("aa_industry_planner:set_plan_purchase", args=[plan.pk, 2])
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(purchase_url, {"action": "buy"}).status_code == 302
        bought = client.get(detail_url).context["cost"]
        assert bought["rows"]["All inputs"] == [Estimate(530), Estimate(741)]
        assert bought["rows"]["Missing inputs to buy"] == [
            Estimate(515),
            Estimate(Decimal("720.5")),
        ]
        assert bought["rows"]["Production fees"] == [Estimate(34)]
        assert bought["steps"]["buy"]["1"]["cost"] == 564
        assert bought["steps"]["sell"]["1"]["cost"] == 775
        assert "2" not in bought["steps"]["sell"]
        post.assert_called_once()

        post.side_effect = Timeout()
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(refresh_url).status_code == 302
        failed = client.get(detail_url)
        assert failed.context["cost"]["rows"] == bought["rows"]
        assert failed.context["cost"]["fetched_at"] == costs["fetched_at"]
        assert b"Showing the last saved quotes" in failed.content
        assert post.call_count == 2


def test_shared_intermediate_cost_rolls_up_by_units_consumed_through_multiple_tiers(
    user, types, assigned_facility
):
    add_recipe(types, 1, {2: 1, 3: 1})
    add_recipe(types, 2, {4: 3})
    add_recipe(types, 3, {4: 4})
    add_recipe(types, 4, {5: 10}, quantity=4)
    source = InventorySource.objects.create(user=user, kind="manual", name="On hand")
    ManualStock.objects.create(
        source=source, item_type=types[4], facility=assigned_facility, quantity=2
    )
    SystemCostIndex.objects.create(
        solar_system=assigned_facility.solar_system,
        activity="manufacturing",
        cost_index=Decimal(".1"),
    )
    for tid in (2, 3, 4, 5):
        AdjustedPrice.objects.create(type_id=tid, adjusted_price=100)
    quotes = {
        "buy_prices": {"1": "900", "2": "250", "3": "250", "4": "300", "5": "10"},
        "sell_prices": {"1": "1200", "2": "400", "3": "450", "4": "400", "5": "20"},
    }
    _, snapshot = calculate_targets(user, {1: 1, 4: 1}, default_me=0)
    costs = plan_cost(dict(snapshot, market_prices=quotes))
    assert costs["rows"]["All inputs"] == [Estimate(500), Estimate(800)]
    assert costs["rows"]["Missing inputs to buy"] == [Estimate(200), Estimate(400)]
    assert costs["rows"]["Total production cost"] == [Estimate(912), Estimate(1212)]
    buy = costs["steps"]["buy"]
    assert buy["4"]["cost"] == 485
    assert buy["2"]["cost"] == Decimal("463.25")
    assert buy["3"]["cost"] == Decimal("299.50")
    assert buy["1"]["cost"] == Decimal("790.75")
    assert costs["steps"]["sell"]["1"]["cost"] == Decimal("1040.75")

    ItemPolicy.objects.create(user=user, item_type=types[4], buy=True)
    _, snapshot = calculate_targets(user, {1: 1, 4: 1}, default_me=0)
    costs = plan_cost(dict(snapshot, market_prices=quotes))
    assert costs["rows"]["All inputs"] == [Estimate(2100), Estimate(2800)]
    assert costs["rows"]["Missing inputs to buy"] == [Estimate(1800), Estimate(2400)]
    assert costs["steps"]["buy"]["1"]["cost"] == 2227
    assert "4" not in costs["steps"]["buy"]


def test_saved_sell_only_quotes_and_unknown_fees_keep_incomplete_profitability_unavailable(
    user, types, assigned_facility, client
):
    add_recipe(types, 1, {2: 1, 5: 4})
    add_recipe(types, 2, {3: 1})
    plan = create_plan(user, 1, 1, use_inventory=False)
    plan.snapshot["market_prices"] = {"prices": {"1": "900", "2": "250", "5": "10"}}
    plan.save(update_fields=["snapshot"])
    client.force_login(user)
    page = client.get(reverse("aa_industry_planner:plan", args=[plan.pk]))
    costs = page.context["cost"]
    assert costs["rows"]["All inputs"] == [Estimate(None, 2), Estimate(40, 1)]
    assert costs["rows"]["Total production cost"] == [Estimate(None, 4), Estimate(40, 3)]
    assert costs["steps"]["sell"]["1"] == {"output": 900, "cost": None, "ratio": None}
    assert b"Known subtotal" in page.content
    assert b"Jita sell Unavailable" not in page.content
