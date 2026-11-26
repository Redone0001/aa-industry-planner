from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from requests import Timeout

from aa_industry_planner.models import AdjustedPrice, InventorySource, ManualStock, SystemCostIndex
from aa_industry_planner.services.planning import create_plan

from .conftest import add_recipe


def test_explicit_refresh_saves_quotes_for_the_buy_list_and_preserves_them_on_failure(
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
        assert initial.context["cost"]["fees"] == 81
        post.assert_not_called()

        post.return_value.status_code = 200
        post.return_value.json.return_value = [
            {"itemType": {"eid": 3}, "immediatePrices": {"sellPrice": 180}},
            {"itemType": {"eid": 5}, "immediatePrices": {"sellPrice": Decimal("20.5")}},
            {"itemType": {"eid": 2}, "immediatePrices": {"sellPrice": 700}},
        ]
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(refresh_url).status_code == 302
        assert post.call_args.kwargs["data"] == b"3\n5"
        assert post.call_args.kwargs["params"] == {"market": 2}
        assert post.call_args.kwargs["headers"]["X-ApiKey"] == "test-janice-key"
        detail = client.get(detail_url)
        assert detail.content.index(b'id="production-timing"') < detail.content.index(
            b'id="production-cost"'
        )
        assert b"test-janice-key" not in detail.content
        assert detail.context["cost"]["input_cost"] == 262
        assert detail.context["cost"]["total"] == 343
        post.assert_called_once()

        purchase_url = reverse("aa_industry_planner:set_plan_purchase", args=[plan.pk, 2])
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(purchase_url, {"action": "buy"}).status_code == 302
        partial = client.get(detail_url)
        assert partial.context["cost"]["input_cost"] == Decimal("20.5")
        assert partial.context["cost"]["missing_inputs"] == 1
        assert b"Known subtotal" in partial.content
        post.assert_called_once()

        post.side_effect = Timeout()
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(refresh_url).status_code == 302
        failed = client.get(detail_url)
        assert failed.context["cost"]["input_cost"] == Decimal("20.5")
        assert failed.context["cost"]["fetched_at"] == detail.context["cost"]["fetched_at"]
        assert b"Showing the last saved quotes" in failed.content
        assert post.call_count == 2
